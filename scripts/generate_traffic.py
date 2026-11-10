"""Send synthetic gameplay traffic to the local TracePilot Compose stack."""

import argparse
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
from itertools import cycle
import json
import math
import os
import random
import signal
import sys
import time
from uuid import UUID, uuid4


LOOPBACK = "127.0.0.1"
HTTP_TIMEOUT = 5
FAILURE_PATH = "/dev/failure"


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("expected a port from 1 to 65535")
    return number


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=positive_number, default=60, help="traffic duration in seconds (default: 60)")
    parser.add_argument("--rate", type=positive_number, default=5, help="maximum Game Server requests per second (default: 5)")
    parser.add_argument("--players", type=positive_integer, default=6, help="number of synthetic players (default: 6)")
    parser.add_argument("--game-port", type=port_number, default=os.getenv("GAME_SERVER_PORT", "8000"))
    parser.add_argument("--processor-port", type=port_number, default=os.getenv("EVENT_PROCESSOR_PORT", "8001"))
    parser.add_argument("--failure-burst", type=positive_number, metavar="SECONDS",
                        help="opt in to a processor_error burst midway through the run")
    args = parser.parse_args()
    if args.failure_burst is not None and args.failure_burst >= args.duration:
        parser.error("--failure-burst must be shorter than --duration")
    return args


def request(connection, method, path, payload=None):
    headers = {"Connection": "close"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    body = json.dumps(payload) if payload is not None else None
    try:
        # HTTPConnection connects directly and returns redirects as responses.
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        # A later request may arrive after the server's keep-alive timeout.
        connection.close()


def match_events(player_id):
    match = {
        "player_id": player_id,
        "match_id": str(uuid4()),
        "map": random.choice(("foundry", "harbor", "outpost")),
        "mode": random.choice(("ranked", "casual")),
    }
    yield {"event_type": "player_joined", "payload": match}
    yield {"event_type": "match_started", "payload": {**match, "rounds": 3}}
    score = 0
    for round_number in range(1, 4):
        score += random.choice((10, 20, 30, 50))
        yield {"event_type": "score_updated", "payload": {**match, "round": round_number, "score": score}}
    yield {"event_type": "match_ended", "payload": {**match, "final_score": score}}
    yield {"event_type": "player_left", "payload": {**match, "reason": "match_complete"}}


@dataclass
class Player:
    id: str
    session_id: UUID | None = None
    events: deque = field(default_factory=deque)

    def reset(self):
        self.session_id = None
        self.events.clear()


@dataclass
class Totals:
    attempts: int = 0
    sessions: int = 0
    events: int = 0
    simulated: int = 0
    failures: Counter = field(default_factory=Counter)

    def report(self, run_id, elapsed):
        success = self.sessions + self.events
        failures = self.simulated + self.failures.total()
        print(f"Run {run_id} | {self.attempts} requests | {success} successful "
              f"({self.sessions} sessions, {self.events} events) | {failures} failures | {elapsed:.1f}s elapsed")
        if unconfirmed := self.attempts - success - failures:
            print(f"  Unconfirmed requests: {unconfirmed}")
        if self.simulated:
            print(f"  Simulated HTTP 502: {self.simulated}")
        if self.failures:
            print("  Unexpected failures: " + ", ".join(
                f"{reason}: {count}" for reason, count in self.failures.most_common()
            ))


def send_turn(game, player, totals, *, injecting=False):
    creating_session = player.session_id is None
    totals.attempts += 1
    try:
        if creating_session:
            status, body = request(game, "POST", "/sessions", {"player_id": player.id})
        else:
            status, body = request(game, "POST", f"/sessions/{player.session_id}/events", player.events[0])

        if injecting and status == 502:
            totals.simulated += 1
            return
        if status != 201:
            totals.failures[f"HTTP {status}"] += 1
            if injecting:
                raise RuntimeError(f"Expected HTTP 502 during the failure burst, received HTTP {status}")
            player.reset()
            return
        if creating_session:
            player.session_id = UUID(json.loads(body)["id"])
            player.events.extend(match_events(player.id))
            totals.sessions += 1
        else:
            totals.events += 1
            player.events.popleft()
            if not player.events:
                player.reset()
        if injecting:
            raise RuntimeError("An event succeeded during the processor failure burst")
    except (OSError, HTTPException, KeyError, TypeError, ValueError) as exc:
        totals.failures[type(exc).__name__] += 1
        if injecting:
            raise RuntimeError("Could not send an event during the failure burst") from exc
        # Start a new synthetic session next turn rather than replay an ambiguous write.
        player.reset()


def run_phase(seconds, rate, game, players, totals, *, next_request=0, injecting=False):
    now = time.monotonic()
    deadline = now + seconds
    next_request = max(now, next_request)
    interval = 1 / rate
    while next_request < deadline:
        time.sleep(max(0, next_request - time.monotonic()))
        started = time.monotonic()
        if started >= deadline:
            break
        send_turn(game, next(players), totals, injecting=injecting)
        next_request = started + interval
    time.sleep(max(0, deadline - time.monotonic()))
    return next_request


def set_failure(processor, config):
    status, body = request(processor, "PUT", FAILURE_PATH, config)
    if status != 200 or json.loads(body) != config:
        raise RuntimeError(f"Event Processor did not confirm failure mode {config['mode']} (HTTP {status})")


@contextmanager
def processor_failure(processor):
    status, body = request(processor, "GET", FAILURE_PATH)
    if status != 200:
        raise RuntimeError(f"Cannot read development failure control (HTTP {status})")
    previous = json.loads(body)
    if previous["mode"] != "none":
        raise RuntimeError(f"Event Processor already has failure mode {previous['mode']} enabled")
    try:
        set_failure(processor, {**previous, "mode": "processor_error"})
        yield
    finally:
        # The server might have accepted a PUT even if its response was lost.
        try:
            set_failure(processor, previous)
        except (OSError, HTTPException, RuntimeError, ValueError) as exc:
            raise RuntimeError("Could not restore the Event Processor failure setting") from exc


def run(args, players, totals):
    regular = cycle(players)
    game = HTTPConnection(LOOPBACK, args.game_port, timeout=HTTP_TIMEOUT)
    if args.failure_burst is None:
        run_phase(args.duration, args.rate, game, regular, totals)
        return

    healthy_seconds = (args.duration - args.failure_burst) / 2
    next_request = run_phase(healthy_seconds, args.rate, game, regular, totals)
    active = [player for player in players if player.session_id is not None]
    if not active:
        raise RuntimeError("No active sessions for the failure burst. Allow more time for warm-up.")
    processor = HTTPConnection(LOOPBACK, args.processor_port, timeout=HTTP_TIMEOUT)
    with processor_failure(processor):
        next_request = run_phase(
            args.failure_burst, args.rate, game, cycle(active), totals,
            next_request=next_request, injecting=True,
        )
    if not totals.simulated:
        raise RuntimeError("The failure burst was too short to send an event at this request rate")
    run_phase(healthy_seconds, args.rate, game, regular, totals, next_request=next_request)


def main():
    args = parse_args()
    run_id = uuid4().hex
    players = [Player(f"traffic-{run_id}-{index}") for index in range(1, args.players + 1)]
    totals = Totals()
    started = time.monotonic()
    try:
        run(args, players, totals)
    except KeyboardInterrupt:
        print("Traffic generation interrupted.", file=sys.stderr)
        return 130
    except (OSError, HTTPException, RuntimeError, ValueError, KeyError) as exc:
        print(f"Traffic generation stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        totals.report(run_id, time.monotonic() - started)
    return int(bool(totals.failures) or not (totals.sessions or totals.events))


if __name__ == "__main__":
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    sys.exit(main())
