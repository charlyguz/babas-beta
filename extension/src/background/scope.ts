import { BabasError } from '../shared/protocol';
import { addBabasTab, getBabasTabs, getCurrentTabId, getGroupId, removeBabasTab, setCurrentTabId, setGroupId } from './state';
import { getTab } from './tabs';

const GROUP_TITLE = 'BABAS';

function isAdoptable(tab: chrome.tabs.Tab): boolean {
  if (tab.id === undefined) return false;
  const url = tab.url || tab.pendingUrl || '';
  return !/^(chrome-extension|devtools):/i.test(url);
}

function recent(tabs: chrome.tabs.Tab[]): chrome.tabs.Tab | undefined {
  return tabs.filter(isAdoptable).sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0))[0];
}

export async function pickWorkingTab(windowId?: number): Promise<chrome.tabs.Tab | undefined> {
  const [active] = await chrome.tabs.query(windowId !== undefined ? { active: true, windowId } : { active: true, lastFocusedWindow: true });
  if (active && isAdoptable(active)) return active;
  return recent(await chrome.tabs.query({}));
}

export async function groupTab(tabId: number): Promise<void> {
  try {
    const tab = await chrome.tabs.get(tabId);
    const known = await getGroupId();
    if (known !== null) {
      try {
        const group = await chrome.tabGroups.get(known);
        if (group.windowId === tab.windowId) {
          await chrome.tabs.group({ groupId: known, tabIds: [tabId] });
          return;
        }
      } catch {
        // the group was closed: make a new one
      }
    }
    const created = await chrome.tabs.group({ tabIds: [tabId], createProperties: { windowId: tab.windowId } });
    await chrome.tabGroups.update(created, { title: GROUP_TITLE, color: 'blue' });
    await setGroupId(created);
  } catch {
    // grouping is cosmetic; some windows cannot hold tab groups
  }
}

export async function adoptTab(tabId: number): Promise<void> {
  await addBabasTab(tabId);
  await setCurrentTabId(tabId);
  await groupTab(tabId);
}

export async function adoptForTask(windowId: number | undefined): Promise<chrome.tabs.Tab> {
  let tab = await pickWorkingTab(windowId);
  if (!tab || tab.id === undefined) {
    tab = await chrome.tabs.create({ url: 'about:blank', active: true });
  }
  if (tab.id === undefined) throw new BabasError('INTERNAL', 'Could not create a tab');
  await adoptTab(tab.id);
  return tab;
}

export async function resolveWorkingTab(): Promise<chrome.tabs.Tab> {
  const current = await getCurrentTabId();
  if (current !== null) {
    if ((await getBabasTabs()).includes(current)) {
      try {
        return await getTab(current);
      } catch {
        await removeBabasTab(current);
      }
    } else {
      await setCurrentTabId(null);
    }
  }
  const picked = await pickWorkingTab();
  if (!picked || picked.id === undefined) throw new BabasError('TAB_NOT_FOUND', 'There is no tab to work on');
  await adoptTab(picked.id);
  return picked;
}

export async function ownedTab(tabId: number): Promise<chrome.tabs.Tab> {
  if (!(await getBabasTabs()).includes(tabId)) {
    throw new BabasError('TAB_NOT_FOUND', `Tab ${tabId} is not one of the tabs BABAS works on`);
  }
  return getTab(tabId);
}
