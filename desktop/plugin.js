import { Button, DisclosureCaret, host, icons, Popover, PopoverContent, PopoverTrigger, TRANSCRIPT_DIRECTIVE_AREA, useQuery, useQueryClient, useValue } from '@hermes/plugin-sdk';
import { createContext, useContext, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';
const PLUGIN_ID = 'mem-diffs';
const DIRECTIVE = 'mem-diff';
const REVIEW_DIRECTIVE = 'mem-diffs-review';
const RECEIPT_ID = /^[0-9a-f]{32}$/;
export const GEOMETRY = { gap: 6, margin: 8, maxWidth: 720, minWidth: 280, panelCap: 480, panelCapVh: 0.6, minPanel: 96 };
// Rectangles use viewport coordinates. natural.height contains the full content height.
export function placePopover({ trigger, bound, viewport, natural }) {
    const g = GEOMETRY;
    const left0 = Math.max(g.margin, bound.left);
    const right0 = Math.min(viewport.width - g.margin, bound.right);
    const maxWidth = Math.max(0, Math.min(right0 - left0, g.maxWidth));
    const minWidth = Math.min(g.minWidth, maxWidth);
    const width = Math.min(Math.max(natural.width, minWidth), maxWidth);
    const below = Math.max(0, viewport.height - trigger.bottom - g.gap - g.margin);
    const above = Math.max(0, trigger.top - g.gap - g.margin);
    const desired = Math.min(natural.height, g.panelCap, Math.floor(viewport.height * g.panelCapVh));
    const side = desired <= below || desired > above ? 'bottom' : 'top';
    let maxHeight = desired;
    let sideOffset = g.gap;
    if (side === 'bottom' && desired > below) {
        const minPanel = Math.min(g.minPanel, desired, viewport.height - 2 * g.margin);
        maxHeight = Math.max(below, minPanel);
        // The panel extends over the trigger when the space above and below is too
        // small. The overlap keeps the header and close control within the viewport.
        if (below < minPanel)
            sideOffset = g.gap - (minPanel - below);
    }
    maxHeight = Math.floor(maxHeight);
    const left = Math.min(Math.max(trigger.left, left0), Math.max(left0, right0 - width));
    return {
        side,
        maxWidth,
        minWidth,
        width,
        maxHeight,
        sideOffset,
        height: Math.min(natural.height, maxHeight),
        fits: natural.height <= maxHeight,
        left,
        alignOffset: Math.round(left - trigger.left)
    };
}
export function findColumnBound(el) {
    const vw = window.innerWidth;
    let node = el?.parentElement;
    while (node && node.parentElement && node !== document.body) {
        const display = getComputedStyle(node).display;
        if (!display.startsWith('inline') && display !== 'contents') {
            const rect = node.getBoundingClientRect();
            const parent = node.parentElement.getBoundingClientRect();
            if (rect.width > 0 && rect.width < parent.width - 1)
                return { left: rect.left, right: rect.right };
        }
        node = node.parentElement;
    }
    return { left: 0, right: vw };
}
const OwnerScope = createContext(null);
function classify(error) {
    if (error?.code === -32601 || error?.unsupported || /method not found|unknown method|not supported/i.test(String(error?.message)))
        return 'unsupported';
    if (error?.status === 404)
        return 'not_found';
    return 'error';
}
async function requestData(owner, sessionId, path) {
    if (typeof host.profileRoutes !== 'function' || typeof host.requestProfile !== 'function')
        throw Object.assign(new Error('Owner routing is not supported by this Desktop version'), { unsupported: true });
    const routes = (await host.profileRoutes()).filter(route => route.connectionId === owner.connectionId && route.profile === owner.profile);
    // An owner hint is not a full route. Never fall back to the ambient gateway.
    if (routes.length !== 1 || !routes[0].mode || !routes[0].targetProfile)
        throw new Error('The chat owner route is unavailable or ambiguous');
    const result = await host.requestProfile(routes[0], 'plugin.mem-diffs.data', { path: path.replace(/^\//, ''), session_id: sessionId });
    if (!result || typeof result.status !== 'number')
        throw new Error('Invalid MemDiffs data response');
    if (result.status !== 200)
        throw Object.assign(new Error('Could not load MemDiffs data'), { status: result.status });
    return result.body;
}
function useData(path, enabled = true) {
    const scope = useContext(OwnerScope);
    const query = useQuery({
        queryKey: [PLUGIN_ID, scope?.connectionId, scope?.profile, scope?.sessionId, path],
        queryFn: () => requestData(scope, scope.sessionId, path),
        enabled: Boolean(scope && enabled),
        // Do not inherit a host's keepPreviousData policy across owners.
        placeholderData: () => undefined,
        retry: (count, error) => classify(error) === 'error' && count < 2,
        retryDelay: count => Math.min(500 * (count + 1), 2000),
        staleTime: 0,
        refetchOnMount: 'always',
        refetchOnWindowFocus: 'always',
        refetchOnReconnect: 'always'
    });
    return [{ status: query.isError ? classify(query.error) : query.data ? 'ready' : 'loading', data: query.data, error: query.error }, () => query.refetch()];
}
function OwnedDirective({ sessionId, id, streaming, review }) {
    const owner = useValue(host.state.focusedSessionOwner);
    const storedId = useValue(host.state.focusedStoredSessionId);
    const runtimeId = useValue(host.state.focusedSessionId);
    const gateway = useValue(host.state.gateway);
    const client = useQueryClient();
    const [opened, setOpened] = useState(null);
    // TranscriptDirectiveRenderProps supplies no immutable owner. Only the
    // focused transcript can be bound safely with the current public SDK.
    const matched = sessionId && (sessionId === storedId || sessionId === runtimeId);
    const safe = matched && owner?.connectionId && owner?.profile;
    const key = JSON.stringify([owner?.connectionId, owner?.profile, sessionId]);
    useEffect(() => {
        if (!safe) return;
        const refresh = () => client.invalidateQueries({ queryKey: [PLUGIN_ID, owner.connectionId, owner.profile, sessionId] }, { cancelRefetch: false });
        refresh();
        window.addEventListener('focus', refresh);
        return () => window.removeEventListener('focus', refresh);
    }, [client, key, safe, gateway]);
    // Native tile focus can start settings loading before row activation.
    // Keep disclosure state across loading and bind an open panel once.
    // Close bound panels when their connection, profile, or session changes.
    const open = Boolean(opened && (!opened.key || (safe && opened.key === key)));
    useEffect(() => {
        if (safe && opened && !opened.key) setOpened({ key });
        else if (opened?.key && (!safe || opened.key !== key)) setOpened(null);
    }, [safe, key, opened]);
    if (!RECEIPT_ID.test(id ?? '')) return null;
    return jsx(OwnerScope.Provider, {
        value: safe ? { ...owner, sessionId } : null,
        children: jsx(review ? MemDiffsReviewRow : MemDiffChip, { sessionId, id, streaming, open, onOpenChange: next => setOpened(next ? { key: safe ? key : null } : null) })
    });
}
const T = {
    text: 'var(--ui-text-primary)',
    secondary: 'var(--ui-text-secondary)',
    muted: 'var(--ui-text-tertiary)',
    stroke: 'var(--ui-stroke-tertiary)',
    hover: 'color-mix(in srgb, var(--ui-text-primary) 7%, transparent)',
    mono: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace)'
};
function Icon({ name, size = 12 }) {
    const Glyph = icons?.[name];
    return Glyph ? jsx(Glyph, { size, 'aria-hidden': true, style: { flex: 'none' } }) : null;
}
const rtf = typeof Intl !== 'undefined' ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }) : null;
function ago(seconds) {
    const delta = Math.round(seconds - Date.now() / 1000);
    const steps = [
        [60, 'second'],
        [3600, 'minute', 60],
        [86400, 'hour', 3600],
        [Infinity, 'day', 86400]
    ];
    for (const [limit, unit, div = 1] of steps) {
        if (Math.abs(delta) < limit)
            return rtf ? rtf.format(Math.round(delta / div), unit) : '';
    }
    return '';
}
const REASONS = {
    too_large: 'Too large to show',
    binary: 'Binary file',
    unreadable: 'Couldn’t read one version of this file',
    symlink: 'Symbolic link (not followed)',
    not_compared: 'Not compared (file limit reached)'
};
function receiptLabel(r) {
    return `Saved change: ${r.title}, ${r.additions ?? 0} additions, ${r.deletions ?? 0} deletions`;
}
let closeOpenViewer = null;
function Viewer({ trigger, triggerShell, inline = false, title, children, boundOf, label, open: controlledOpen, defaultOpen = false, onOpenChange, flush = false }) {
    const [localOpen, setOpenState] = useState(defaultOpen);
    const open = controlledOpen ?? localOpen;
    const panelOpen = open && !inline;
    const changeRef = useRef(onOpenChange);
    changeRef.current = onOpenChange;
    const setOpen = useCallback(next => {
        setOpenState(next);
        changeRef.current?.(next);
    }, []);
    const [place, setPlace] = useState(null);
    const triggerRef = useRef(null);
    const headerRef = useRef(null);
    const innerRef = useRef(null);
    const frameRef = useRef(null);
    const headingRef = useRef(null);
    const frame = useRef(0);
    const observer = useRef(null);
    const headingId = useId();
    // The portal mounts popover content after the popover opens. Callback refs attach
    // nodes to the active observer if the nodes mount after the effect.
    const observe = ref => el => {
        if (ref.current && observer.current)
            observer.current.unobserve(ref.current);
        ref.current = el;
        if (el && observer.current)
            observer.current.observe(el);
    };
    const innerRefCb = useCallback(observe(innerRef), []);
    const headerRefCb = useCallback(observe(headerRef), []);
    const close = useCallback(() => setOpen(false), [setOpen]);
    const measure = useCallback(() => {
        frame.current = 0;
        const t = triggerRef.current;
        const inner = innerRef.current;
        const box = frameRef.current;
        if (!t || !inner || !box)
            return;
        const tr = t.getBoundingClientRect();
        const vh = window.innerHeight;
        if (tr.bottom < 0 || tr.top > vh) {
            setOpen(false);
            return;
        }
        const naturalHeight = (headerRef.current?.offsetHeight ?? 0) + inner.offsetHeight + 2;
        const next = placePopover({
            trigger: tr,
            bound: boundOf(t),
            viewport: { width: window.innerWidth, height: vh },
            natural: { width: box.offsetWidth + 2, height: naturalHeight }
        });
        next.natural = Math.round(naturalHeight);
        setPlace(prev => prev &&
            prev.natural === next.natural &&
            prev.side === next.side &&
            Math.abs(prev.maxHeight - next.maxHeight) < 1 &&
            Math.abs(prev.maxWidth - next.maxWidth) < 1 &&
            Math.abs(prev.alignOffset - next.alignOffset) < 1 &&
            prev.sideOffset === next.sideOffset
            ? prev
            : next);
    }, [boundOf, setOpen]);
    const schedule = useCallback(() => {
        if (!frame.current)
            frame.current = requestAnimationFrame(measure);
    }, [measure]);
    useLayoutEffect(() => {
        if (!panelOpen) {
            setPlace(null);
            return undefined;
        }
        measure();
        const ro = new ResizeObserver(schedule);
        observer.current = ro;
        for (const el of [innerRef.current, headerRef.current, triggerRef.current])
            if (el)
                ro.observe(el);
        window.addEventListener('resize', schedule);
        document.addEventListener('scroll', schedule, true);
        return () => {
            ro.disconnect();
            observer.current = null;
            window.removeEventListener('resize', schedule);
            document.removeEventListener('scroll', schedule, true);
            if (frame.current)
                cancelAnimationFrame(frame.current);
            frame.current = 0;
        };
    }, [panelOpen, measure, schedule]);
    useEffect(() => {
        if (!panelOpen)
            return undefined;
        if (closeOpenViewer && closeOpenViewer !== close)
            closeOpenViewer();
        closeOpenViewer = close;
        return () => {
            if (closeOpenViewer === close)
                closeOpenViewer = null;
        };
    }, [panelOpen, close]);
    const contentStyle = {
        padding: 0,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        boxSizing: 'border-box',
        width: 'max-content',
        minWidth: place ? `${place.minWidth}px` : undefined,
        maxWidth: place ? `${place.maxWidth}px` : `${GEOMETRY.minWidth}px`,
        maxHeight: place ? `${place.maxHeight}px` : '1px',
        // The panel uses opacity to keep keyboard focus possible before Viewer measures
        // the panel.
        opacity: place ? 1 : 0,
        color: T.text
    };
    const triggerNode = jsx(PopoverTrigger, { asChild: true, children: trigger({ ref: triggerRef, open }) });
    return jsxs(Popover, {
        open: panelOpen,
        onOpenChange: setOpen,
        children: [
            triggerShell ? triggerShell(triggerNode) : triggerNode,
            jsx(PopoverContent, {
                variant: 'menu',
                side: place?.side ?? 'bottom',
                align: 'start',
                sideOffset: place?.sideOffset ?? GEOMETRY.gap,
                alignOffset: place?.alignOffset ?? 0,
                // placePopover selects the panel side; do not let the SDK change it.
                avoidCollisions: false,
                'aria-labelledby': headingId,
                'aria-label': label,
                'data-mem-diffs-viewer': '',
                'data-side-chosen': place?.side,
                'data-natural-height': place?.natural,
                style: contentStyle,
                onOpenAutoFocus: event => {
                    event.preventDefault();
                    headingRef.current?.focus({ preventScroll: true });
                },
                children: jsxs('div', {
                    ref: frameRef,
                    style: { display: 'flex', flexDirection: 'column', minHeight: 0, maxHeight: 'inherit', width: '100%' },
                    children: [
                        jsxs('div', {
                            ref: headerRefCb,
                            style: {
                                flex: 'none',
                                display: 'flex',
                                alignItems: 'center',
                                gap: 6,
                                padding: '6px 6px 6px 10px',
                                borderBottom: `1px solid ${T.stroke}`
                            },
                            children: [
                                jsx('div', {
                                    ref: headingRef,
                                    id: headingId,
                                    tabIndex: -1,
                                    style: {
                                        ...SCAFFOLD_LABEL,
                                        flex: '1 1 auto',
                                        minWidth: 0,
                                        fontWeight: 600,
                                        color: T.text,
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                        outline: 'none'
                                    },
                                    children: title
                                }),
                                jsx(Button, {
                                    variant: 'ghost',
                                    size: 'icon',
                                    'aria-label': 'Close',
                                    title: 'Close',
                                    onClick: close,
                                    style: { width: 24, height: 24, flex: 'none' },
                                    children: jsx(Icon, { name: 'X', size: 14 })
                                })
                            ]
                        }),
                        jsx('div', {
                            'data-viewer-body': '',
                            style: { flex: '1 1 auto', minHeight: 0, overflowY: 'auto', overflowX: 'hidden' },
                            children: jsx('div', {
                                ref: innerRefCb,
                                style: { padding: flush ? 0 : 8, fontSize: 'var(--conversation-tool-font-size, 0.75rem)', color: T.secondary },
                                children: open ? children : null
                            })
                        })
                    ]
                })
            })
        ]
    });
}
function StateLine({ children, onRetry }) {
    return jsxs('div', {
        role: 'status',
        style: { display: 'flex', alignItems: 'center', gap: 8, color: T.muted, lineHeight: 1.5 },
        children: [
            jsx('span', { style: { flex: '1 1 auto' }, children }),
            onRetry &&
                jsx(Button, { variant: 'outline', size: 'sm', onClick: onRetry, children: 'Retry' })
        ]
    });
}
function LoadFailure({ state, retry, message }) {
    return jsx(StateLine, {
        onRetry: state.status === 'unsupported' || state.status === 'not_found' ? undefined : retry,
        children: state.status === 'unsupported'
            ? 'MemDiffs owner-routed data is unavailable. Update or enable the backend plugin on this chat’s owner.'
            : state.status === 'not_found' ? 'This change record isn’t available here. It may belong to another profile or have been deleted.' : message
    });
}
function Skeleton() {
    return jsx('div', {
        'aria-busy': true,
        'aria-label': 'Loading',
        style: { display: 'grid', gap: 6 },
        children: [70, 90, 55].map(w => jsx('div', { key: w, style: { height: 10, width: `${w}%`, borderRadius: 4, background: T.hover } }))
    });
}
export function parseDiffLines(text) {
    const out = [];
    let inHunk = false;
    let emitted = false;
    for (const line of String(text ?? '').replace(/\n$/, '').split('\n')) {
        if (line.startsWith('@@')) {
            if (emitted)
                out.push({ kind: 'context', text: '' });
            inHunk = true;
            continue;
        }
        if (!inHunk || line.startsWith('\\'))
            continue;
        const kind = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'remove' : 'context';
        out.push({ kind, text: kind !== 'context' || line.startsWith(' ') ? line.slice(1) : line });
        emitted = true;
    }
    return out;
}
const DIFF_TINT = {
    add: { borderColor: 'var(--ui-diff-add-border)', background: 'var(--ui-diff-add-background)', color: 'var(--ui-diff-add-foreground)' },
    remove: { borderColor: 'var(--ui-diff-remove-border)', background: 'var(--ui-diff-remove-background)', color: 'var(--ui-diff-remove-foreground)' },
    context: { borderColor: 'transparent' }
};
function DiffBlock({ text, cap = false }) {
    const lines = parseDiffLines(text);
    return jsx('div', {
        'data-diff': '',
        role: 'region',
        'aria-label': 'Diff',
        tabIndex: 0,
        style: {
            maxWidth: '100%',
            minWidth: 0,
            maxHeight: cap ? '12rem' : undefined,
            overflow: 'auto',
            overscrollBehaviorY: 'auto',
            overscrollBehaviorX: 'contain',
            fontFamily: T.mono,
            fontSize: '0.7rem',
            lineHeight: 1.625,
            color: 'var(--ui-text-secondary)',
            outline: 'none'
        },
        children: lines.map((line, i) => jsx('span', {
            'data-diff-line': line.kind,
            style: {
                display: 'block',
                minWidth: 'max-content',
                whiteSpace: 'pre',
                borderLeft: '2px solid',
                padding: '1px 10px',
                ...DIFF_TINT[line.kind]
            },
            children: line.text || ' '
        }, i))
    });
}
function DiffStats({ additions = 0, deletions = 0 }) {
    if (!additions && !deletions)
        return null;
    return jsxs('span', {
        'data-diff-stats': '',
        style: { display: 'inline-flex', flex: 'none', alignItems: 'center', gap: 4, fontFamily: T.mono, fontSize: '0.625rem', fontVariantNumeric: 'tabular-nums' },
        children: [
            additions > 0 && jsx('span', { className: 'text-emerald-600 dark:text-emerald-400', children: `+${additions}` }, 'a'),
            deletions > 0 && jsx('span', { className: 'text-rose-600 dark:text-rose-400', children: `−${deletions}` }, 'd')
        ]
    });
}
const META = { fontSize: '0.625rem', fontVariantNumeric: 'tabular-nums', color: 'var(--conversation-scaffold-meta)' };
const SCAFFOLD_LABEL = {
    fontSize: 'var(--conversation-tool-font-size, 0.75rem)',
    lineHeight: 'var(--conversation-line-height, 1.25rem)'
};
export function displayPath(path, rels) {
    const p = String(path ?? '');
    if (p.startsWith('memories/'))
        return p.slice('memories/'.length);
    for (const rel of rels ?? []) {
        if (rel && p.endsWith('/' + rel)) {
            const head = p.slice(0, -rel.length - 1).split('/');
            return `${head[head.length - 1]}/${rel}`;
        }
    }
    return p.replace(/^skills\//, '');
}
const FILE_HEAD = {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    minWidth: 0,
    padding: '3px 8px 3px 12px',
    fontSize: 'var(--conversation-tool-font-size, 0.75rem)',
    lineHeight: 'var(--conversation-line-height, 1.25rem)'
};
function FileDiff({ file, rels, first, cap }) {
    const reason = file.reason ? REASONS[file.reason] ?? 'Unavailable' : null;
    return jsxs('section', {
        'data-file': file.status,
        style: { minWidth: 0, borderTop: first ? undefined : `1px solid ${T.stroke}` },
        children: [
            jsxs('div', {
                'data-file-head': '',
                style: FILE_HEAD,
                children: [
                    jsx('span', {
                        title: file.path,
                        style: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--conversation-scaffold-text, var(--ui-text-secondary))' },
                        children: displayPath(file.path, rels)
                    }),
                    file.status === 'added' && jsx('span', { style: { ...META, flex: 'none' }, children: 'new' }),
                    file.status === 'deleted' && jsx('span', { style: { ...META, flex: 'none' }, children: 'deleted' }),
                    file.diff != null && jsx(DiffStats, { additions: file.additions, deletions: file.deletions })
                ]
            }),
            file.diff != null
                ? jsx(DiffBlock, { text: file.diff, cap })
                : jsx('div', { style: { padding: '0 8px 6px 12px' }, children: jsx(StateLine, { children: reason ?? 'No textual diff' }) })
        ]
    });
}
const PAD = { padding: '6px 8px 6px 12px' };
function ReceiptFiles({ sessionId, id, rels, first = true, cap = false }) {
    const [state, retry] = useData(`/receipts/${id}`, RECEIPT_ID.test(id ?? ''));
    const edge = first ? undefined : `1px solid ${T.stroke}`;
    if (state.status === 'loading')
        return jsx('div', { style: { ...PAD, borderTop: edge }, children: jsx(Skeleton, {}) });
    if (state.status === 'not_found')
        return jsx('div', {
            style: { ...PAD, borderTop: edge },
            children: jsx(StateLine, { children: 'This change record isn’t available here. It may belong to another profile or have been deleted.' })
        });
    if (state.status === 'error' || state.status === 'unsupported')
        return jsx('div', { style: { ...PAD, borderTop: edge }, children: jsx(LoadFailure, { state, retry, message: 'Couldn’t load this change.' }) });
    const r = state.data.receipt;
    const notes = [
        r.uncertain && 'An earlier version couldn’t be read, so part of this change is shown without a diff.',
        r.concurrent && 'Another save touched the same files at the same time; this diff may include its changes.',
        r.truncated && 'Some files were omitted or not shown (size limit).'
    ].filter(Boolean);
    return jsxs('div', {
        'data-receipt': r.id ?? id,
        style: { minWidth: 0 },
        children: [
            ...r.files.map((file, i) => jsx(FileDiff, { file, rels, first: first && i === 0, cap }, `${i}:${file.path}`)),
            ...notes.map(note => jsx('div', { style: { ...META, fontSize: '0.6875rem', ...PAD, paddingTop: 2 }, children: note }, note))
        ]
    });
}
function ReceiptDetail({ sessionId, id }) {
    return jsx(ReceiptFiles, { sessionId, id });
}
const BRAIN_PATH = 'M248,124a56.11,56.11,0,0,0-32-50.61V72a48,48,0,0,0-88-26.49A48,48,0,0,0,40,72v1.39a56,56,0,0,0,0,101.22V176a48,48,0,0,0,88,26.49A48,48,0,0,0,216,176v-1.39A56.09,56.09,0,0,0,248,124ZM88,208a32,32,0,0,1-31.81-28.56A55.87,55.87,0,0,0,64,180h8a8,8,0,0,0,0-16H64A40,40,0,0,1,51.2,86.1a8,8,0,0,0,5.6-6.83A32,32,0,0,1,120,72v75.36A47.79,47.79,0,0,0,88,136a8,8,0,0,0,0,16,32,32,0,0,1,32,32A24,24,0,0,1,88,208Zm104-44h-8a8,8,0,0,0,0,16h8a55.87,55.87,0,0,0,7.81-.56A32,32,0,1,1,136,184a32,32,0,0,1,32-32,8,8,0,0,0,0-16,47.79,47.79,0,0,0-32,11.36V72a32,32,0,0,1,63.2-7.27,8,8,0,0,0,5.6,6.83A40,40,0,0,1,192,164Z';
const counterStyle = pending => ({
    all: 'unset',
    boxSizing: 'border-box',
    display: 'flex',
    alignItems: 'center',
    width: 'fit-content',
    minHeight: 'var(--conversation-line-height, 1.25rem)',
    padding: '0 2px',
    margin: '0 0 0 -2px',
    borderRadius: 4,
    cursor: pending ? 'default' : 'pointer'
});
const counterText = r => (r.additions || r.deletions ? null : '±0');
function MemDiffChip({ id, sessionId, streaming, open, onOpenChange }) {
    const [state, retry] = useData(`/receipts/${id}`, RECEIPT_ID.test(id ?? ''));
    const summary = state.status === 'ready' ? state.data.receipt : { title: 'Saved change' };
    const columnOf = useCallback(el => findColumnBound(el), []);
    if (state.status === 'error' || state.status === 'unsupported')
        return jsx(LoadFailure, { state, retry, message: 'Couldn’t load this change.' });
    if (!RECEIPT_ID.test(id ?? '') || state.status === 'not_found')
        return null;
    const counter = state.status !== 'ready'
        ? jsx('span', { style: META, children: 'Saved change' })
        : counterText(summary)
        ? jsx('span', { style: META, children: counterText(summary) })
        : jsx(DiffStats, { additions: summary.additions, deletions: summary.deletions });
    if (streaming)
        return jsx('span', {
            'data-mem-diff-chip': '',
            'data-conversation-scaffold': '',
            'aria-disabled': true,
            title: summary.title,
            style: counterStyle(true),
            children: counter
        });
    return jsx(Viewer, {
        open, onOpenChange,
        label: receiptLabel(summary),
        title: summary.title,
        boundOf: columnOf,
        trigger: ({ ref, open }) => jsx('button', {
            ref,
            type: 'button',
            'data-mem-diff-chip': '',
            'data-conversation-scaffold': '',
            'aria-haspopup': 'dialog',
            'aria-expanded': open,
            'aria-label': receiptLabel(summary),
            title: summary.title,
            style: counterStyle(false),
            children: counter
        }),
        flush: true,
        children: jsx(ReceiptDetail, { sessionId, id })
    });
}

const REVIEW_LABEL = 'Self-improvement review';
function BatchBody({ sessionId, group, cap }) {
    const files = new Map((group.receipts ?? []).map(r => [r.id, r.files]));
    return jsx('div', {
        'data-batch': group.id,
        style: { minWidth: 0 },
        children: group.receipt_ids.map((id, i) => jsx(ReceiptFiles, { sessionId, id, rels: files.get(id), first: i === 0, cap }, id))
    });
}
function reviewRow(summary, pending) {
    return [
        jsx('span', {
            className: pending ? undefined : 'tool-memory-legendary-glyph',
            style: {
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: '0.875rem',
                height: 'var(--conversation-line-height, 1.25rem)',
                flex: 'none'
            },
            children: jsx('svg', {
                'aria-hidden': true,
                viewBox: '0 0 256 256',
                fill: 'currentColor',
                style: {
                    width: '0.875rem',
                    height: '0.875rem',
                    flex: 'none',
                    color: pending ? 'var(--ui-text-tertiary)' : 'var(--tool-memory-legendary-icon)'
                },
                children: jsx('path', { d: BRAIN_PATH })
            })
        }, 'g'),
        jsx('span', {
            className: pending ? undefined : 'tool-memory-legendary-title',
            style: { ...SCAFFOLD_LABEL, flex: 'none', color: pending ? 'var(--conversation-scaffold-meta)' : 'transparent' },
            children: REVIEW_LABEL
        }, 'l'),
        summary &&
            jsx('span', {
                className: 'tool-memory-legendary-meta',
                style: { ...SCAFFOLD_LABEL, minWidth: 0, overflowWrap: 'anywhere', textAlign: 'left' },
                children: summary
            }, 'd')
    ];
}
const reviewRowStyle = pending => ({
    all: 'unset',
    boxSizing: 'border-box',
    display: 'flex',
    alignItems: 'flex-start',
    gap: 6,
    width: '100%',
    minWidth: 0,
    maxWidth: '100%',
    padding: '2px 0',
    cursor: pending ? 'default' : 'pointer'
});
const SHELL_CLASS = 'rounded-[0.3125rem] border border-(--ui-stroke-tertiary)';
const HEAD_OPEN_CLASS = 'border-b border-(--ui-stroke-tertiary) px-2 py-1.5';
const ROW_CLASS = 'group/disclosure-row relative flex w-full max-w-full min-w-0 text-(--ui-text-tertiary)';
const ROW_BUTTON_CLASS = 'flex min-w-0 max-w-fit items-start gap-1.5 text-left transition-colors hover:text-foreground focus-visible:text-foreground';
const CARET_CLASS = 'flex h-(--conversation-line-height) shrink-0 items-center justify-center transition-opacity duration-150';
const CARET_REST_CLASS = 'opacity-(--disclosure-caret-rest) group-hover/disclosure-row:opacity-80 group-focus-within/disclosure-row:opacity-80';

function Review({ sessionId, group, summary, label, open, onOpenChange, popover }) {
    const bodyId = useId();
    const columnOf = useCallback(el => findColumnBound(el), []);
    const inlineOpen = open && !popover;
    const n = group?.receipt_ids.length ?? 0;
    return jsx(Viewer, {
        open, onOpenChange,
        label,
        flush: true,
        title: jsxs('span', {
            style: { display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 0 },
            children: [
                jsx('span', { className: 'tool-memory-legendary-title', style: { color: 'transparent' }, children: REVIEW_LABEL }),
                jsx('span', { style: { ...META, fontWeight: 400 }, children: `${n} change${n === 1 ? '' : 's'}` })
            ]
        }),
        boundOf: columnOf,
        inline: !popover,
        triggerShell: trigger => jsxs('div', {
            'data-mem-diffs-review-card': '',
            'data-open': inlineOpen ? '' : undefined,
            className: inlineOpen ? SHELL_CLASS : undefined,
            style: { minWidth: 0, maxWidth: '100%', overflow: 'hidden', padding: popover || inlineOpen ? 0 : '2px 0' },
            children: [
                jsx('div', {
                    className: inlineOpen ? HEAD_OPEN_CLASS : undefined,
                    children: jsx('div', { className: ROW_CLASS, children: trigger })
                }),
                inlineOpen && jsx('div', { id: bodyId, role: 'region', 'aria-label': label, children: group ? jsx(BatchBody, { sessionId, group, cap: true }) : jsx(Skeleton, {}) })
            ]
        }),
        trigger: ({ ref }) => jsxs('button', {
            ref,
            type: 'button',
            'data-mem-diffs-review': '',
            'aria-haspopup': popover ? 'dialog' : undefined,
            'aria-expanded': open,
            'aria-controls': popover ? undefined : bodyId,
            'aria-label': label,
            className: popover ? undefined : ROW_BUTTON_CLASS,
            style: popover ? reviewRowStyle(false) : { cursor: 'pointer' },
            onClick: event => {
                if (!popover) {
                    event.preventDefault(); // Inline disclosure, not the Radix dialog.
                    onOpenChange(!open);
                }
            },
            children: [
                ...reviewRow(summary, false),
                !popover && jsx('span', { className: `${CARET_CLASS} ${open ? 'opacity-80' : CARET_REST_CLASS}`, children: jsx(DisclosureCaret, { open }) }, 'c')
            ]
        }),
        children: group ? jsx(BatchBody, { sessionId, group, cap: false }) : jsx(Skeleton, {})
    });
}
function MemDiffsReviewRow({ id, sessionId, streaming, open, onOpenChange }) {
    const valid = RECEIPT_ID.test(id ?? '');
    const [state, retry] = useData(`/review-groups/${id}`, valid);
    const [settings, retrySettings] = useData('/settings', valid);
    const popover = settings.status === 'ready' && settings.data?.settings?.review_popover === true;
    if (!valid)
        return null;
    if (state.status === 'error' || state.status === 'unsupported')
        return jsx(LoadFailure, { state, retry, message: 'Couldn’t load this review.' });
    if (state.status === 'ready' && settings.status !== 'ready' && settings.status !== 'loading')
        return jsx(LoadFailure, { state: settings, retry: retrySettings, message: 'Couldn’t load review settings.' });
    const group = state.status === 'ready' ? state.data.group : null;
    const summary = group?.summary ?? null;
    const pending = streaming || !sessionId || state.status === 'not_found' || (group && !group.receipt_ids?.length);
    if (pending)
        return jsx('span', {
            'data-mem-diffs-review': '',
            'aria-disabled': true,
            style: reviewRowStyle(true),
            children: reviewRow(summary, true)
        });
    const label = `${REVIEW_LABEL}: ${summary ?? (group ? `${group.receipt_ids.length} saved changes` : 'Saved changes')}`;
    return jsx(Review, { sessionId, group, summary, label, open, onOpenChange, popover });
}
export default {
    id: PLUGIN_ID,
    name: 'MemDiffs',
    register(ctx) {

        ctx.register({
            // This renderer shows stored mem-diff directives. New foreground saves
            // use diffs in tool results.
            id: 'directive',
            area: TRANSCRIPT_DIRECTIVE_AREA,
            data: {
                name: DIRECTIVE,
                render: ({ attrs, sessionId, streaming }) => jsx(OwnedDirective, { id: attrs?.id, sessionId: sessionId ?? null, streaming: Boolean(streaming) })
            }
        });
        ctx.register({
            id: 'review-directive',
            area: TRANSCRIPT_DIRECTIVE_AREA,
            data: {
                name: REVIEW_DIRECTIVE,
                render: ({ attrs, sessionId, streaming }) => jsx(OwnedDirective, { review: true, id: attrs?.id, sessionId: sessionId ?? null, streaming: Boolean(streaming) })
            }
        });
        ctx.onDispose(() => closeOpenViewer?.());
    }
};
