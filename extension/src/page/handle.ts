import { BabasError, toErrorInfo, type PageCommand, type PageResponse } from '../shared/protocol';
import { click, mark, press, readText, scroll, selectOption, typeText, unmark, waitIdle } from './actions';
import { visibleBox } from './dom';
import { clearHighlights, highlightAll, highlightTarget } from './highlight';
import { resolveTarget, tryResolve } from './resolve';
import { currentSnapshot, describeElement, takeSnapshot } from './snapshot';

async function run(cmd: PageCommand): Promise<unknown> {
  switch (cmd.op) {
    case 'ping':
      return { pong: true, url: location.href };
    case 'snapshot': {
      const state = takeSnapshot(cmd.max_elements ?? 250, cmd.max_text ?? 2000);
      if (cmd.highlight) highlightAll(currentSnapshot().map);
      return state;
    }
    case 'describe': {
      const el = resolveTarget(cmd.target);
      const index = cmd.target.index ?? -1;
      return { element: describeElement(el, index), host: location.hostname, url: location.href };
    }
    case 'exists': {
      const el = tryResolve(cmd.target);
      return { exists: !!el, visible: !!(el && visibleBox(el)) };
    }
    case 'click': {
      const el = resolveTarget(cmd.target);
      highlightTarget(el);
      return click(el, cmd.authorized);
    }
    case 'type': {
      const el = resolveTarget(cmd.target);
      highlightTarget(el);
      return typeText(el, cmd.text, cmd.authorized);
    }
    case 'press': {
      const el = cmd.target ? resolveTarget(cmd.target) : null;
      return press(cmd.key, el, cmd.authorized);
    }
    case 'select': {
      const el = resolveTarget(cmd.target);
      highlightTarget(el);
      return selectOption(el, cmd.value, cmd.authorized);
    }
    case 'scroll':
      return scroll(cmd.direction, cmd.amount);
    case 'read':
      return readText(cmd.target ? resolveTarget(cmd.target) : null, cmd.max_chars);
    case 'highlight':
      if (cmd.mode === 'off') clearHighlights();
      else if (cmd.mode === 'all') highlightAll(currentSnapshot().map);
      else if (cmd.target) highlightTarget(resolveTarget(cmd.target));
      return { mode: cmd.mode };
    case 'idle':
      return waitIdle(cmd.quiet_ms, cmd.timeout_ms);
    case 'mark':
      return mark(resolveTarget(cmd.target));
    case 'unmark':
      unmark();
      return {};
    default:
      throw new BabasError('BAD_REQUEST', `Unknown page op: ${(cmd as { op?: string }).op}`);
  }
}

export async function handle(cmd: PageCommand): Promise<PageResponse> {
  try {
    return { ok: true, result: await run(cmd) };
  } catch (err) {
    return { ok: false, error: toErrorInfo(err) };
  }
}
