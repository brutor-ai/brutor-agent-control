/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, {
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
} from 'react';
import {
    Play,
    Pause,
    AlertCircle,
    Lock,
    Eraser,
    RefreshCw,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../ui/tableStyles';
import {
    LogLevel,
    LogLevelEntry,
    LogLevelListResponse,
    SetLogLevelRequest,
} from '../../lib/systemOpsTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemLogsPane.
 *
 * Three concerns in one pane:
 *   1. Per-module level sliders that POST to the gateway and update
 *      the persisted override row. `system:operate` gates the change;
 *      a read-only operator sees the current state but every slider
 *      is disabled with a tooltip pointing to the required permission.
 *   2. A virtualised live tail of the local replica's logs (WebSocket
 *      to /v1/admin/system/logs/tail). 'Pause' freezes auto-scroll
 *      without dropping the connection — operators want to inspect a
 *      backlog block, then resume without losing the cursor position.
 *   3. A connection-state line — replica id surfaced
 *      so an operator chasing a noisy request knows which pod they're
 *      tailing right now. The tail follows one replica; it does not
 *      fan out over several.
 *
 * Virtualisation strategy:
 *   We deliberately use a plain max-height auto-scroll container with
 *   `overflow-y-auto` + a render cap (last MAX_RENDERED lines) rather
 *   than `react-window` / `@tanstack/react-virtual`. The reason is
 *   pragmatic: the tail's hot path is JSON-decode + append, not DOM
 *   shuffling — at the operator-perceived rate (a few hundred lines/s
 *   on a busy proxy) cropping to the last MAX_RENDERED in the React
 *   list is already faster than rebuilding a virtualiser's row map
 *   per batch. If we hit hard p99s during an incident, the right
 *   future answer is to add @tanstack/react-virtual to the dep tree;
 *   the line-state model already separates render from buffer so the
 *   swap is local to this file.
 *
 * Permission posture:
 *   - `system:read`  → connect + render sliders (read-only).
 *   - `system:operate` → modify level sliders.
 *   The backend is the authoritative gate; the UI hints at the right
 *   intent so a curl-armed user without the permission still gets a
 *   clear 403 reason.
 */

const PERM_READ = 'system:read';
const PERM_OPERATE = 'system:operate';

const LEVELS: LogLevel[] = ['trace', 'debug', 'info', 'warn', 'error'];

// The render cap. We hold every received line in `buffer` (capped at
// BUFFER_CAP) so Pause doesn't lose history; the visible list is the
// tail slice. BUFFER_CAP × visible-row-cost stays bounded so even a
// log storm doesn't pin the browser.
const BUFFER_CAP = 5000;
const MAX_RENDERED = 1000;

interface TailLine {
    seq: number;
    ts: string;
    level: string;
    logger: string;
    message: string;
    dropped_since_last?: number;
}

interface HelloLine {
    type: 'hello';
    replica_id: string;
    ring_size: number;
    initial_replay: number;
}


/**
 * What a close of the log tail other than a normal one means. The server
 * closes with 1008 and one of two reasons when it refuses a ticket: one
 * that is spent, expired or another administrator's ("invalid or expired
 * ticket"), and a session that ended after the ticket was minted ("session
 * ended"). Any other reason is shown as the server gives it.
 */
function tailCloseSentence(code: number, reason: string): string {
    if (reason === 'invalid or expired ticket') {
        return 'The log tail was refused: its ticket was spent or had expired. Reload the page to open it again.';
    }
    if (reason === 'session ended') {
        return 'The log tail closed: the session it was opened in has ended. Sign in again to follow the log.';
    }
    return `Tail connection closed: ${reason ? `${reason} (code ${code})` : `code ${code}`}`;
}

const SystemLogsPane: React.FC = () => {
    const { hasPermission } = useAuth();
    const { gatewayClient } = useGateway();
    const canOperate = hasPermission(PERM_OPERATE);
    const canRead = hasPermission(PERM_READ);

    // ─── Levels state ────────────────────────────────────────────
    const [levels, setLevels] = useState<LogLevelEntry[]>([]);
    const [levelsError, setLevelsError] = useState<string | null>(null);
    const [savingModule, setSavingModule] = useState<string | null>(null);

    // ─── Tail state ──────────────────────────────────────────────
    const [paused, setPaused] = useState(false);
    const [connected, setConnected] = useState(false);
    const [tailError, setTailError] = useState<string | null>(null);
    const [replicaId, setReplicaId] = useState<string | null>(null);
    const [droppedTotal, setDroppedTotal] = useState(0);

    // `buffer` is the authoritative growing list — drives the render
    // and any "save to file" intent. `viewport` is the React-state
    // mirror, kept tail-bounded so renders stay fast.
    const bufferRef = useRef<TailLine[]>([]);
    const [viewport, setViewport] = useState<TailLine[]>([]);
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const wsRef = useRef<WebSocket | null>(null);

    // ─── Levels: fetch on mount ──────────────────────────────────
    const fetchLevels = useCallback(async () => {
        try {
            const resp: LogLevelListResponse =
                await gatewayClient.getLogLevels();
            setLevels(resp.items);
            setLevelsError(null);
        } catch (e) {
            setLevelsError(errorMessage(e) || String(e));
        }
    }, [gatewayClient]);

    // `fetchLevels` is intentionally omitted — gatewayClient identity
    // flips each render, which would loop the effect.
     
    useEffect(() => {
        if (canRead) runBackground(fetchLevels(), 'levels');
    }, [canRead]);

    // ─── Levels: mutate a single module ──────────────────────────
    const setLevel = useCallback(
        async (module: string, level: LogLevel) => {
            if (!canOperate) return;
            setSavingModule(module);
            try {
                const body: SetLogLevelRequest = {
                    module,
                    level,
                    scope: 'all',
                };
                await gatewayClient.setLogLevel(body);
                await fetchLevels();
            } catch (e) {
                setLevelsError(errorMessage(e) || String(e));
            } finally {
                setSavingModule(null);
            }
        },
        [canOperate, fetchLevels, gatewayClient],
    );

    // ─── Tail: WebSocket lifecycle ───────────────────────────────
    useEffect(() => {
        if (!canRead) return;
        let cancelled = false;
        let ws: WebSocket | null = null;

        const attach = (socket: WebSocket) => {
            ws = socket;
            wsRef.current = socket;
            socket.onopen = () => {
                setConnected(true);
                setTailError(null);
            };
            socket.onmessage = ev => {
                try {
                    const parsed = JSON.parse(ev.data);
                    if (parsed && parsed.type === 'hello') {
                        const hello = parsed as HelloLine;
                        setReplicaId(hello.replica_id);
                        return;
                    }
                    const line = parsed as TailLine;
                    if (typeof line.seq !== 'number') return;
                    if (line.dropped_since_last) {
                        setDroppedTotal(t => t + (line.dropped_since_last ?? 0));
                    }
                    // Append into the ref buffer first — this is what
                    // 'export' / 'pause' should reflect. Cap so a long
                    // tail session doesn't unbounded-grow memory.
                    const buf = bufferRef.current;
                    buf.push(line);
                    if (buf.length > BUFFER_CAP) {
                        buf.splice(0, buf.length - BUFFER_CAP);
                    }
                    // Reflect into React state. We tail-slice so the DOM
                    // stays small even during a log storm.
                    setViewport(buf.slice(-MAX_RENDERED));
                } catch {
                    // A frame that is not JSON is dropped.
                }
            };
            socket.onerror = () => {
                // The event says nothing by the specification, and its
                // target is the socket: it is not written. What went wrong
                // arrives on onclose, as the server's close code and reason.
                console.error('[system-logs] WS error');
            };
            socket.onclose = ev => {
                setConnected(false);
                // Code 1000 is a normal close: no error then. Any other
                // close says why, in the server's words or in a sentence
                // for the two reasons the server gives a ticket's refusal.
                const code = ev?.code ?? 0;
                if (code !== 1000 && code !== 1005) {
                    setTailError(tailCloseSentence(code, (ev?.reason || '').trim()));
                }
                console.error(`[system-logs] WS closed with code ${code}`);
            };
        };

        const open = async () => {
            let socket: WebSocket | null;
            try {
                socket = await gatewayClient.openLogsWebSocket();
            } catch (e) {
                if (!cancelled) setTailError(`The log tail could not be opened: ${errorMessage(e)}`);
                return;
            }
            if (cancelled) {
                socket?.close();
                return;
            }
            if (!socket) {
                setTailError(
                    'Log tail unavailable — the API client could not establish a WebSocket connection.',
                );
                return;
            }
            attach(socket);
        };
        runBackground(open(), 'tail');

        return () => {
            cancelled = true;
            try {
                ws?.close();
            } catch {
                /* noop */
            }
            wsRef.current = null;
        };
        // gatewayClient identity flips each render — depending on it
        // would tear down and re-open the WebSocket on every render.
        // canRead is the only effective trigger we care about.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [canRead]);

    // ─── Tail: pause/resume sync to backend ──────────────────────
    useEffect(() => {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        try {
            ws.send(JSON.stringify({ op: paused ? 'pause' : 'resume' }));
        } catch {
            /* socket may have closed mid-toggle — handler will surface */
        }
    }, [paused]);

    // ─── Tail: auto-scroll unless paused ─────────────────────────
    useEffect(() => {
        if (paused) return;
        const el = scrollRef.current;
        if (!el) return;
        // Why rAF: the new tail line is appended to viewport in a
        // setState; the DOM hasn't laid out yet. Defer the scroll to
        // the next frame so we land on the actual new bottom, not
        // the pre-append bottom.
        const id = requestAnimationFrame(() => {
            el.scrollTop = el.scrollHeight;
        });
        return () => cancelAnimationFrame(id);
    }, [viewport, paused]);

    const clearBuffer = useCallback(() => {
        bufferRef.current = [];
        setViewport([]);
        setDroppedTotal(0);
    }, []);

    // ─── Render ──────────────────────────────────────────────────
    if (!canRead) {
        // Defence-in-depth — the parent permission gate already
        // hides this pane, but a deep-link could still land here.
        return (
            <div className="text-secondary text-sm">
                The Logs page requires the <code>system:read</code> permission.
            </div>
        );
    }

    return (
        <div className="flex flex-col gap-6 max-w-6xl">
            {/* ── Level sliders ─────────────────────────────────── */}
            <section data-testid="system-logs-levels">
                <header className="flex items-center justify-between mb-3">
                    <div>
                        <h2 className="text-base font-semibold text-default">
                            Module log levels
                        </h2>
                        <p className="text-xs text-secondary">
                            Adjust per-module dispatch level at runtime.
                            Changes apply on every replica and
                            persist across restarts.
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={asyncHandler(fetchLevels, 'click')}
                        data-testid="system-logs-levels-refresh"
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                    >
                        <RefreshCw className="w-4 h-4" />
                        Refresh
                    </button>
                </header>

                {levelsError && (
                    <div
                        data-testid="system-logs-levels-error"
                        className="mb-3 bg-red-50 border border-red-200 text-red-800 p-3 rounded-md text-sm flex items-start gap-2"
                    >
                        <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                        <span>{levelsError}</span>
                    </div>
                )}

                <div className="border border-default rounded-md overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className={TABLE_THEAD}>
                            <tr className="text-left">
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Module</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Level</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Override</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {levels.length === 0 ? (
                                <tr>
                                    <td
                                        colSpan={3}
                                        className="px-3 py-4 text-secondary italic"
                                        data-testid="system-logs-levels-empty"
                                    >
                                        No log modules to display.
                                    </td>
                                </tr>
                            ) : (
                                levels.map(entry => (
                                    <LevelRow
                                        key={entry.module}
                                        entry={entry}
                                        canOperate={canOperate}
                                        saving={savingModule === entry.module}
                                        onChange={asyncHandler(setLevel, 'change')}
                                    />
                                ))
                            )}
                        </tbody>
                    </table>
                </div>
            </section>

            {/* ── Tail viewer ───────────────────────────────────── */}
            <section data-testid="system-logs-tail">
                <header className="flex items-center justify-between mb-3 gap-3 flex-wrap">
                    <div>
                        <h2 className="text-base font-semibold text-default">
                            Live tail
                        </h2>
                        <p className="text-xs text-secondary">
                            Streaming one replica's logs. Refresh to land on a different replica.
                        </p>
                    </div>
                    <div className="flex items-center gap-2">
                        <ConnectionBadge
                            connected={connected}
                            replicaId={replicaId}
                        />
                        {droppedTotal > 0 && (
                            <span
                                data-testid="system-logs-dropped"
                                className="text-xs px-2 py-1 rounded bg-amber-100 text-amber-800 border border-amber-300"
                            >
                                {droppedTotal} dropped
                            </span>
                        )}
                        <button
                            type="button"
                            onClick={() => setPaused(p => !p)}
                            data-testid="system-logs-pause-toggle"
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                        >
                            {paused ? (
                                <>
                                    <Play className="w-4 h-4" />
                                    Resume
                                </>
                            ) : (
                                <>
                                    <Pause className="w-4 h-4" />
                                    Pause
                                </>
                            )}
                        </button>
                        <button
                            type="button"
                            onClick={clearBuffer}
                            data-testid="system-logs-clear"
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                        >
                            <Eraser className="w-4 h-4" />
                            Clear
                        </button>
                    </div>
                </header>

                {tailError && (
                    <div
                        data-testid="system-logs-tail-error"
                        className="mb-3 bg-red-50 border border-red-200 text-red-800 p-3 rounded-md text-sm flex items-start gap-2"
                    >
                        <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                        <span>{tailError}</span>
                    </div>
                )}

                <div
                    ref={scrollRef}
                    data-testid="system-logs-tail-viewport"
                    className="bg-black/95 text-emerald-200 font-mono text-xs leading-relaxed rounded-md border border-default p-3 overflow-y-auto h-[480px]"
                >
                    {viewport.length === 0 ? (
                        <div className="text-emerald-700 italic">
                            Waiting for log lines…
                        </div>
                    ) : (
                        viewport.map(line => (
                            <TailLineView key={line.seq} line={line} />
                        ))
                    )}
                </div>
            </section>
        </div>
    );
};

// ─── Level row ──────────────────────────────────────────────────

interface LevelRowProps {
    entry: LogLevelEntry;
    canOperate: boolean;
    saving: boolean;
    onChange: (module: string, level: LogLevel) => void;
}

const LevelRow: React.FC<LevelRowProps> = ({
    entry,
    canOperate,
    saving,
    onChange,
}) => {
    const drift =
        entry.override_level !== null && entry.override_level !== entry.level;
    return (
        <tr>
            <td className="px-3 py-2">
                <code className="text-default">{entry.module}</code>
                {!entry.is_curated && (
                    <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-surface-muted text-tertiary">
                        ad-hoc
                    </span>
                )}
            </td>
            <td className="px-3 py-2">
                <div
                    role="radiogroup"
                    aria-label={`Level for ${entry.module}`}
                    className="inline-flex rounded-md border border-default overflow-hidden"
                >
                    {LEVELS.map(lvl => {
                        const active = entry.level === lvl;
                        return (
                            <button
                                key={lvl}
                                type="button"
                                role="radio"
                                aria-checked={active}
                                disabled={!canOperate || saving}
                                data-testid={`system-logs-level-${entry.module}-${lvl}`}
                                title={
                                    canOperate
                                        ? `Set ${entry.module} to ${lvl}`
                                        : `Requires system:operate`
                                }
                                onClick={() => onChange(entry.module, lvl)}
                                className={`px-2.5 py-1 text-xs font-medium ${
                                    active
                                        ? 'bg-primary-500 text-white'
                                        : 'bg-surface text-secondary hover:bg-surface-muted'
                                } ${
                                    !canOperate
                                        ? 'opacity-50 cursor-not-allowed'
                                        : ''
                                }`}
                            >
                                {lvl}
                            </button>
                        );
                    })}
                </div>
                {!canOperate && (
                    <Lock className="inline-block w-3 h-3 ml-2 text-tertiary" />
                )}
            </td>
            <td className="px-3 py-2">
                {entry.override_level ? (
                    <span
                        className={`text-xs ${drift ? 'text-amber-700' : 'text-secondary'}`}
                        title={drift ? 'Persisted override differs from live level' : undefined}
                    >
                        {entry.override_level}
                        {drift && ' (drift)'}
                    </span>
                ) : (
                    <span className="text-xs text-tertiary">—</span>
                )}
            </td>
        </tr>
    );
};

// ─── Connection badge ───────────────────────────────────────────

const ConnectionBadge: React.FC<{
    connected: boolean;
    replicaId: string | null;
}> = ({ connected, replicaId }) => (
    <span
        data-testid="system-logs-connection-badge"
        className={`inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs border ${
            connected
                ? 'bg-emerald-50 border-emerald-300 text-emerald-800'
                : 'bg-slate-100 border-slate-300 text-slate-700'
        }`}
    >
        <span
            className={`w-1.5 h-1.5 rounded-full ${
                connected ? 'bg-emerald-500' : 'bg-slate-400'
            }`}
        />
        {connected ? 'connected' : 'disconnected'}
        {replicaId && <span className="text-tertiary">· {replicaId}</span>}
    </span>
);

// ─── Tail line view ─────────────────────────────────────────────

const TailLineView: React.FC<{ line: TailLine }> = ({ line }) => {
    const ts = useMemo(() => {
        try {
            return new Date(line.ts).toLocaleTimeString();
        } catch {
            return line.ts;
        }
    }, [line.ts]);
    return (
        <div className="whitespace-pre-wrap break-all">
            <span className="text-emerald-500">{ts}</span>{' '}
            <span className={levelClass(line.level)}>{line.level.toUpperCase()}</span>{' '}
            <span className="text-emerald-400">{line.logger}</span>{' '}
            <span>{line.message}</span>
        </div>
    );
};

function levelClass(level: string): string {
    switch (level) {
        case 'error':
            return 'text-red-400 font-semibold';
        case 'warn':
            return 'text-amber-300 font-semibold';
        case 'info':
            return 'text-emerald-200';
        case 'debug':
            return 'text-emerald-400';
        case 'trace':
            return 'text-emerald-600';
        default:
            return 'text-emerald-200';
    }
}

export default SystemLogsPane;
