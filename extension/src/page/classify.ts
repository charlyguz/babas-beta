import type { Flag } from '../shared/protocol';
import { formOf, inputType, isContentEditable, isDisabled, isHTMLElement, isTextField, isVisible, labelOf, roleOf } from './dom';
import { detectPii } from './pii';
import { fold, wordCount } from './text';

const CONSEQUENTIAL_VERBS =
  /\b(enviar|envia|send|mandar|submit|entregar|turn in|hand in|pagar|pay|comprar|buy|purchase|checkout|finalizar compra|realizar pedido|place order|eliminar|borrar|delete|publicar|post|publish|firmar|sign|agendar|reservar|book|schedule|confirmar|confirm|transferir|transfer|solicitar|registrarme|registrarse|sign up|crear cuenta|create account|darse de baja|unsubscribe)\b/;
const NOT_CONSEQUENTIAL = /\b(sign in|sign out|log in|log out|login|logout|iniciar sesion|cerrar sesion|acceder)\b/;
const PII_FIELD = /\b(curp|rfc|nss|numero de seguridad social|seguridad social|social security|correo|e-?mail|email address|telefono|tel|phone|celular|cel|movil|whatsapp)\b/;
const PAYMENT_FIELD = /\b(tarjeta|card number|numero de tarjeta|cvv|cvc|cvv2|codigo de seguridad|security code|vencimiento|expiracion|expiration)\b/;
const SEARCH_NAMES = new Set(['q', 's', 'query', 'search', 'search_query', 'keyword', 'keywords', 'buscar', 'busqueda', 'term', 'k']);
const SEARCH_HINT = /(search|buscar|busca|busqueda|find)/;
const CLICKABLE_ROLES = new Set(['button', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'option', 'treeitem']);

export const CAPTCHA_SELECTOR = [
  'iframe[src*="recaptcha"]',
  'iframe[src*="hcaptcha"]',
  'iframe[src*="challenges.cloudflare.com"]',
  'iframe[title*="captcha" i]',
  '.g-recaptcha',
  '.h-captcha',
  '.cf-turnstile',
  '[data-sitekey]',
].join(',');

function attrHint(el: HTMLElement): string {
  return fold(
    [
      el.getAttribute('name'),
      el.id,
      el.getAttribute('placeholder'),
      el.getAttribute('aria-label'),
      el.getAttribute('autocomplete'),
      el.getAttribute('title'),
    ]
      .filter(Boolean)
      .join(' ')
      .replace(/[_-]+/g, ' '),
  );
}

export function isPasswordField(el: HTMLElement): boolean {
  if (inputType(el) === 'password') return true;
  const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
  return autocomplete.includes('password');
}

export function isSearchField(el: HTMLElement): boolean {
  const role = roleOf(el);
  if (role === 'searchbox' || inputType(el) === 'search') return true;
  if (el.closest('[role="search"]')) return true;
  const name = (el.getAttribute('name') || '').toLowerCase();
  if (SEARCH_NAMES.has(name)) return true;
  const hint = attrHint(el);
  if (SEARCH_HINT.test(hint) && !PII_FIELD.test(hint)) return true;
  return role === 'combobox' && SEARCH_HINT.test(fold(labelOf(el)));
}

export function isSearchForm(form: HTMLFormElement): boolean {
  if (form.getAttribute('role') === 'search' || form.closest('[role="search"]')) return true;
  const fields = Array.from(form.elements).filter(
    (f): f is HTMLInputElement | HTMLTextAreaElement => isHTMLElement(f) && isTextField(f) && isVisible(f),
  );
  if (fields.some((f) => isPasswordField(f))) return false;
  if (fields.length !== 1) return false;
  return isSearchField(fields[0]);
}

export function isSubmitControl(el: HTMLElement): boolean {
  const tag = el.tagName.toLowerCase();
  if (tag === 'button') {
    const type = (el.getAttribute('type') || 'submit').toLowerCase();
    return type === 'submit' && !!formOf(el);
  }
  if (tag === 'input') {
    const type = inputType(el);
    return (type === 'submit' || type === 'image') && !!formOf(el);
  }
  return false;
}

export function isConsequentialLabel(label: string): boolean {
  const text = fold(label);
  if (!text || NOT_CONSEQUENTIAL.test(text)) return false;
  if (wordCount(text) <= 6) return CONSEQUENTIAL_VERBS.test(text);
  const firstWords = text.split(' ').slice(0, 3).join(' ');
  return CONSEQUENTIAL_VERBS.test(firstWords);
}

export function fieldPiiKind(el: HTMLElement, label: string): { pii: boolean; payment: boolean } {
  const type = inputType(el);
  const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
  const payment = autocomplete.startsWith('cc-') || PAYMENT_FIELD.test(fold(label)) || PAYMENT_FIELD.test(attrHint(el));
  if (payment) return { pii: true, payment: true };
  if (isSearchField(el)) return { pii: false, payment: false };
  if (type === 'email' || type === 'tel') return { pii: true, payment: false };
  if (autocomplete === 'email' || autocomplete.startsWith('tel')) return { pii: true, payment: false };
  const hint = `${fold(label)} ${attrHint(el)}`;
  return { pii: PII_FIELD.test(hint), payment: false };
}

export function flagsFor(el: HTMLElement, label: string): Flag[] {
  const flags: Flag[] = [];
  const role = roleOf(el);
  const form = formOf(el);
  const searchForm = form ? isSearchForm(form) : false;
  if (isDisabled(el)) flags.push('disabled');
  if (isSubmitControl(el)) flags.push('submit');
  if (searchForm || ((isTextField(el) || isContentEditable(el)) && isSearchField(el))) flags.push('search_form');
  if (isPasswordField(el)) flags.push('password');
  if (isTextField(el) || isContentEditable(el) || el instanceof HTMLSelectElement) {
    const kind = fieldPiiKind(el, label);
    if (kind.pii) flags.push('pii');
    if (kind.payment) flags.push('payment');
  }
  const clickable = CLICKABLE_ROLES.has(role) || flags.includes('submit');
  if (clickable && isConsequentialLabel(label)) flags.push('consequential');
  if (clickable && /\b(pagar|pay|pago|checkout)\b/.test(fold(label))) flags.push('payment');
  if (el.closest(CAPTCHA_SELECTOR) || el.matches(CAPTCHA_SELECTOR)) flags.push('captcha');
  return [...new Set(flags)];
}

export type GateReason =
  | 'submit_form'
  | 'consequential'
  | 'payment'
  | 'pii_field'
  | 'pii_value'
  | 'enter_submits_form'
  | 'enter_in_editor';

export function clickGateReasons(el: HTMLElement): GateReason[] {
  const flags = flagsFor(el, labelOf(el));
  const reasons: GateReason[] = [];
  if (flags.includes('submit') && !flags.includes('search_form')) reasons.push('submit_form');
  if (flags.includes('consequential')) reasons.push('consequential');
  if (flags.includes('payment') && !flags.includes('pii')) reasons.push('payment');
  return reasons;
}

export function typeGateReasons(el: HTMLElement, value: string): GateReason[] {
  const flags = flagsFor(el, labelOf(el));
  const reasons: GateReason[] = [];
  if (flags.includes('pii')) reasons.push('pii_field');
  if (flags.includes('payment')) reasons.push('payment');
  if (detectPii(value).length > 0) reasons.push('pii_value');
  return reasons;
}

export function enterGateReasons(el: HTMLElement | null): GateReason[] {
  if (!el) return [];
  if (isContentEditable(el) || el instanceof HTMLTextAreaElement) {
    return isSearchField(el) ? [] : ['enter_in_editor'];
  }
  const form = formOf(el);
  if (form && isTextField(el)) return isSearchForm(form) ? [] : ['enter_submits_form'];
  return [];
}
