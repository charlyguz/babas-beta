import './micpermission.css';
import { mountPermissionPage, type PermissionPageDeps } from './permission';

const t: PermissionPageDeps['t'] = (key, substitutions) => {
  try {
    return chrome.i18n.getMessage(key, substitutions) || key;
  } catch {
    return key;
  }
};

function start(): void {
  const root = document.getElementById('app');
  if (!root) return;
  try {
    document.documentElement.lang = chrome.i18n.getUILanguage().split('-')[0] || 'es';
  } catch {
    // the static lang attribute stays
  }
  const devices = navigator.mediaDevices;
  mountPermissionPage(root, { t, getUserMedia: devices ? (constraints) => devices.getUserMedia(constraints) : undefined });
}

start();
