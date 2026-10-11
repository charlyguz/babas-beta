import type { PageCommand } from '../shared/protocol';
import { handle } from '../page/handle';

const FLAG = '__babasContentLoaded';
const scope = globalThis as typeof globalThis & { [FLAG]?: boolean };

if (!scope[FLAG]) {
  scope[FLAG] = true;
  chrome.runtime.onMessage.addListener((message: { babas?: PageCommand }, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || !message || !message.babas) return false;
    handle(message.babas).then(sendResponse);
    return true;
  });
}
