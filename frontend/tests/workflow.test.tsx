import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { InvestigationResult } from '../src/api/types';

const result: InvestigationResult = {
  id: 'bf1686f8-52d7-4cd1-96b8-fd2f2d70974a',
  incident: { request_id: 'web-smoke-502', description: 'The event request failed.' },
  status: 'partial',
  started_at: '2026-11-09T05:00:00Z',
  finished_at: '2026-11-09T05:00:15Z',
  duration_ms: 15000,
  suspected_root_cause: null,
  confidence: 'undetermined',
  confirmed_observations: [],
  supporting_evidence: [],
  missing_information: [],
  recommended_next_action: null,
  stop_reason: 'model_unavailable',
  timeline: [],
  evidence: [],
};

function mount(path: string) {
  render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
  return userEvent.setup();
}

function mockApi(handle: (path: URL, init?: RequestInit) => Response | Promise<Response>) {
  const fetch = vi.fn((input: string, init?: RequestInit) => {
    const path = new URL(input, 'http://127.0.0.1:5173');
    if (path.pathname === '/api/health') return Promise.resolve(Response.json({ status: 'ok' }));
    return Promise.resolve().then(() => handle(path, init));
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

describe('new investigation', () => {
  it('validates incident input, waits for the real POST contract, and opens the saved result', async () => {
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const fetch = mockApi((path, init) => {
      if (path.pathname === '/api/investigations' && init?.method === 'POST') return pending;
      if (path.pathname === `/api/investigations/${result.id}`) return Response.json(result);
      throw new Error(`Unexpected request: ${path.pathname}`);
    });
    const user = mount('/investigations/new/');
    const start = screen.getByRole('button', { name: 'Start investigation' });

    await user.click(start);
    expect(screen.getByRole('alert')).toHaveTextContent('Provide an incident description');
    const trace = screen.getByRole('textbox', { name: 'Trace ID' });
    await user.type(trace, '0'.repeat(32));
    await user.click(start);
    expect(trace).toHaveAttribute('aria-invalid', 'true');
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);

    await user.clear(trace);
    await user.type(screen.getByRole('textbox', { name: 'Request ID' }), 'web-smoke-502');
    await user.click(start);
    const running = screen.getByRole('button', { name: 'Investigation running…' });
    expect(running).toBeDisabled();
    expect(screen.getByLabelText('Elapsed time')).toBeVisible();
    await user.click(running);
    const posts = fetch.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe('/api/investigations');
    expect(JSON.parse(posts[0][1]!.body as string)).toEqual({ request_id: 'web-smoke-502' });
    finish(Response.json(result, { status: 201 }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(`/api/investigations/${result.id}`, expect.any(Object)));
    expect(await screen.findByText(/no suspected root cause|undetermined/i, { selector: 'h2, h3, p, strong' })).toBeVisible();
  });

  it('preserves input and offers a manual retry after HTTP 429', async () => {
    mockApi(() => Response.json({ detail: 'An investigation is already running' }, { status: 429 }));
    const user = mount('/investigations/new');
    const description = screen.getByRole('textbox', { name: 'Incident description' });
    await user.type(description, 'The event request failed.');
    await user.click(screen.getByRole('button', { name: 'Start investigation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Another investigation is already running');
    expect(description).toHaveValue('The event request failed.');
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
  });

  it('warns that the server may have received a request after a network error', async () => {
    mockApi(() => { throw new TypeError('Failed to fetch'); });
    const user = mount('/investigations/new');
    await user.type(screen.getByRole('textbox', { name: /additional context/i }), 'A slow write');
    await user.click(screen.getByRole('button', { name: 'Start investigation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The server may have received your request.');
    expect(screen.getByRole('link', { name: 'View saved investigations' })).toBeVisible();
  });

  it('keeps an active submission while browsing history and links to the saved result', async () => {
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const fetch = mockApi((_path, init) => init?.method === 'POST' ? pending : Response.json({ items: [], total: 0, limit: 20, offset: 0 }));
    const user = mount('/investigations/new');
    await user.type(screen.getByRole('textbox', { name: 'Incident description' }), 'Investigate an error');
    await user.click(screen.getByRole('button', { name: 'Start investigation' }));
    await user.click(screen.getByRole('navigation').querySelector('a[href="/investigations"]')!);
    expect(await screen.findByRole('link', { name: 'View status' })).toBeVisible();
    expect(fetch.mock.calls.find(([, init]) => init?.method === 'POST')?.[1]?.signal).toBeUndefined();
    finish(Response.json(result, { status: 201 }));
    expect(await screen.findByRole('link', { name: 'View result' })).toHaveAttribute('href', `/investigations/${result.id}`);
    expect(screen.getByRole('heading', { name: 'Investigations' })).toBeVisible();
  });
});

describe('investigation history', () => {
  it('shows a loading state and an actionable empty state', async () => {
    let finish!: (response: Response) => void;
    mockApi(() => new Promise<Response>((resolve) => { finish = resolve; }));
    mount('/investigations');
    expect(screen.getByText('Loading investigations…')).toBeVisible();
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    finish(Response.json({ items: [], total: 0, limit: 20, offset: 0 }));
    expect(await screen.findByRole('heading', { name: 'No saved investigations' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Investigation history' }).querySelector('a')).toHaveAttribute('href', '/investigations/new');
  });

  it('renders saved data and applies search, status, time order, pagination, and refresh', async () => {
    const query = vi.fn();
    mockApi((path) => {
      const params = Object.fromEntries(path.searchParams);
      query(params);
      return Response.json({ items: [result], total: 21, limit: 20, offset: Number(params.offset) });
    });
    const user = mount('/investigations');
    expect(await screen.findByText('The event request failed.')).toBeVisible();
    expect(screen.getByRole('link', { name: `Open investigation ${result.id}` })).toHaveAttribute('href', `/investigations/${result.id}`);
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(query).toHaveBeenLastCalledWith({ limit: '20', offset: '20', sort: 'newest' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filter by status' }), 'partial');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Sort by time' }), 'oldest');
    await user.type(screen.getByRole('searchbox'), 'web-smoke-502');
    await user.click(screen.getByRole('button', { name: 'Search' }));
    const expected = { limit: '20', offset: '0', sort: 'oldest', status: 'partial', search: 'web-smoke-502' };
    await waitFor(() => expect(query).toHaveBeenLastCalledWith(expected));
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(query.mock.calls.filter(([params]) => params.search === 'web-smoke-502')).toHaveLength(2));
  });
});
