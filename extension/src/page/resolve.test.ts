import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BabasError } from '../shared/protocol';
import { bySelector, parseSelector } from './resolve';
import { byId, mount, stubLayout } from './test-helpers';

describe('parseSelector', () => {
  it('treats text="x" with quotes as an exact text match', () => {
    expect(parseSelector("text='Mostrar mensaje'")).toEqual({ css: '*', text: 'Mostrar mensaje', exact: true });
    expect(parseSelector('text="Mostrar mensaje"')).toEqual({ css: '*', text: 'Mostrar mensaje', exact: true });
  });

  it('treats unquoted text=x as a substring match', () => {
    expect(parseSelector('text=Mostrar')).toEqual({ css: '*', text: 'Mostrar', exact: false });
    expect(parseSelector('text=Ir a B')).toEqual({ css: '*', text: 'Ir a B', exact: false });
  });

  it('trims whitespace around the selector and the value', () => {
    expect(parseSelector("  text='  Hola  '  ")).toEqual({ css: '*', text: '  Hola  ', exact: true });
    expect(parseSelector('  text=  Hola  ')).toEqual({ css: '*', text: 'Hola', exact: false });
  });

  it('parses :has-text with double quotes, single quotes or none', () => {
    expect(parseSelector('button:has-text("Mostrar")')).toEqual({ css: 'button', text: 'Mostrar', exact: false });
    expect(parseSelector("button:has-text('Mostrar')")).toEqual({ css: 'button', text: 'Mostrar', exact: false });
    expect(parseSelector('a:has-text(Ir a B)')).toEqual({ css: 'a', text: 'Ir a B', exact: false });
  });

  it('keeps the CSS part of a compound selector before :has-text', () => {
    expect(parseSelector('form#contact button.primary:has-text("Enviar")')).toEqual({
      css: 'form#contact button.primary',
      text: 'Enviar',
      exact: false,
    });
  });

  it('uses * when :has-text has no CSS part', () => {
    expect(parseSelector(':has-text("Hola")')).toEqual({ css: '*', text: 'Hola', exact: false });
  });

  it('passes plain CSS through untouched', () => {
    expect(parseSelector('#show')).toEqual({ css: '#show' });
    expect(parseSelector('  input[name="curp"]  ')).toEqual({ css: 'input[name="curp"]' });
    expect(parseSelector('form#contact > .row a[href^="#"]')).toEqual({ css: 'form#contact > .row a[href^="#"]' });
  });
});

describe('bySelector', () => {
  beforeEach(() => {
    stubLayout();
    mount(`
      <button id="show" type="button">Mostrar mensaje</button>
      <button id="hide" type="button">Ocultar</button>
      <a id="help" href="#ayuda">Ayuda</a>
      <button id="logout" type="button">Cerrar sesión</button>
      <p id="note">Mostrar es un verbo</p>
      <form id="contact"><input id="nombre" name="nombre"></form>
    `);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });

  it('resolves plain CSS', () => {
    expect(bySelector('#show')).toBe(byId('show'));
    expect(bySelector('form#contact input[name="nombre"]')).toBe(byId('nombre'));
  });

  it('resolves CSS plus :has-text, in both quote styles', () => {
    expect(bySelector("button:has-text('Mostrar')")).toBe(byId('show'));
    expect(bySelector('button:has-text("ocultar")')).toBe(byId('hide'));
  });

  it('resolves text= exactly when quoted and by substring when not', () => {
    expect(bySelector("text='Mostrar mensaje'")).toBe(byId('show'));
    expect(bySelector('text=Ayuda')).toBe(byId('help'));
    expect(() => bySelector("text='Mostrar'")).toThrow(BabasError);
    expect(bySelector('text=mostrar mens')).toBe(byId('show'));
  });

  it('folds case and accents when matching text', () => {
    expect(bySelector("text='AYUDA'")).toBe(byId('help'));
    expect(bySelector('button:has-text("MOSTRAR")')).toBe(byId('show'));
    expect(bySelector("text='cerrar sesion'")).toBe(byId('logout'));
    expect(bySelector('text=SESIÓN')).toBe(byId('logout'));
  });

  it('throws NOT_FOUND when nothing matches', () => {
    expect(() => bySelector('#nope')).toThrowError(expect.objectContaining({ code: 'NOT_FOUND' }));
    expect(() => bySelector('button:has-text("Nada")')).toThrowError(expect.objectContaining({ code: 'NOT_FOUND' }));
  });

  it('throws BAD_REQUEST for an invalid CSS selector', () => {
    expect(() => bySelector('div[')).toThrowError(expect.objectContaining({ code: 'BAD_REQUEST' }));
  });
});
