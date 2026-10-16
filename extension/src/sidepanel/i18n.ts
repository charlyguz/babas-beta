export type Translate = (key: string, substitutions?: string | string[]) => string;

export const t: Translate = (key, substitutions) => {
  try {
    return chrome.i18n.getMessage(key, substitutions) || key;
  } catch {
    return key;
  }
};
