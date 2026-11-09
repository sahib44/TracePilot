BEGIN;

CREATE TABLE game_sessions (
    id UUID PRIMARY KEY,
    player_id VARCHAR(255) NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE game_events (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES game_sessions (id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    request_id VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX game_events_session_id_idx ON game_events (session_id);
CREATE INDEX game_events_request_id_idx ON game_events (request_id);

COMMIT;
