import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clickGateReasons,
  enterGateReasons,
  fieldPiiKind,
  flagsFor,
  isConsequentialLabel,
  isPasswordField,
  isSearchField,
  isSearchForm,
  isSubmitControl,
  typeGateReasons,
} from './classify';
import { labelOf } from './dom';
import { byId, mount, stubLayout } from './test-helpers';

beforeEach(() => {
  stubLayout();
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('isConsequentialLabel', () => {
  it.each(['Enviar', 'Enviar solicitud', 'ENVIAR', 'Cómo pagar', 'Pagar', 'Comprar ahora', 'Eliminar cuenta', 'Confirmar', 'Reservar cita', 'Crear cuenta', 'Registrarme', 'Darse de baja', 'Turn in', 'Sign up', 'Delete', 'Solicitar cita'])(
    '%j is consequential',
    (label) => {
      expect(isConsequentialLabel(label)).toBe(true);
    },
  );

  it.each(['Iniciar sesión', 'Cerrar sesión', 'Acceder', 'Sign in', 'Log out', 'Login', 'Buscar', 'Ver más', 'Ayuda', 'Mostrar mensaje', 'Continuar', 'Aceptar cookies', ''])(
    '%j is not consequential',
    (label) => {
      expect(isConsequentialLabel(label)).toBe(false);
    },
  );

  it('lets the login exclusion win over a consequential verb', () => {
    expect(isConsequentialLabel('Iniciar sesión para enviar')).toBe(false);
    expect(isConsequentialLabel('Acceder y pagar')).toBe(false);
  });

  it('matches whole words only', () => {
    expect(isConsequentialLabel('Postres')).toBe(false);
    expect(isConsequentialLabel('Compartir')).toBe(false);
    expect(isConsequentialLabel('Pagos recientes')).toBe(false);
  });

  it('accepts a verb anywhere in labels of 6 words or fewer', () => {
    expect(isConsequentialLabel('Quiero enviar mi tarea ya')).toBe(true);
  });

  it('only looks at the first 3 words of longer labels', () => {
    expect(isConsequentialLabel('Haga clic aquí para ver cómo puede pagar sus impuestos')).toBe(false);
    expect(isConsequentialLabel('Pagar impuestos del año en curso hoy mismo')).toBe(true);
  });
});

describe('isSubmitControl', () => {
  it('is true for a button with no type inside a form', () => {
    mount('<form><button id="b">Ir</button></form>');
    expect(isSubmitControl(byId('b'))).toBe(true);
  });

  it('is true for an explicit submit button and for submit and image inputs', () => {
    mount('<form><button id="a" type="submit">Ir</button><input id="b" type="submit" value="Ir"><input id="c" type="image" alt="Ir"></form>');
    expect(isSubmitControl(byId('a'))).toBe(true);
    expect(isSubmitControl(byId('b'))).toBe(true);
    expect(isSubmitControl(byId('c'))).toBe(true);
  });

  it('is false for type=button and for reset controls', () => {
    mount('<form><button id="a" type="button">Ir</button><button id="b" type="reset">Borrar</button><input id="c" type="button" value="Ir"></form>');
    expect(isSubmitControl(byId('a'))).toBe(false);
    expect(isSubmitControl(byId('b'))).toBe(false);
    expect(isSubmitControl(byId('c'))).toBe(false);
  });

  it('is false outside a form', () => {
    mount('<button id="a">Ir</button><input id="b" type="submit" value="Ir">');
    expect(isSubmitControl(byId('a'))).toBe(false);
    expect(isSubmitControl(byId('b'))).toBe(false);
  });

  it('is false for links and text fields', () => {
    mount('<form><a id="a" href="#x">Ir</a><input id="b" type="text"><div id="c" role="button">Ir</div></form>');
    expect(isSubmitControl(byId('a'))).toBe(false);
    expect(isSubmitControl(byId('b'))).toBe(false);
    expect(isSubmitControl(byId('c'))).toBe(false);
  });
});

describe('isSearchField and isPasswordField', () => {
  it('recognises search fields by type, role, name, hint and container', () => {
    mount(`
      <input id="a" type="search">
      <input id="b" type="text" role="searchbox">
      <input id="c" type="text" name="q">
      <input id="d" type="text" placeholder="Buscar en el sitio">
      <input id="e" type="text" aria-label="Search">
      <div role="search"><input id="f" type="text" name="algo"></div>
    `);
    for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) expect(isSearchField(byId(id)), id).toBe(true);
  });

  it('does not treat personal-data fields as search fields', () => {
    mount(`
      <input id="a" type="text" name="nombre">
      <input id="b" type="text" placeholder="Buscar por correo">
      <input id="c" type="email" name="correo">
    `);
    for (const id of ['a', 'b', 'c']) expect(isSearchField(byId(id)), id).toBe(false);
  });

  it('recognises password fields by type and by autocomplete', () => {
    mount(`
      <input id="a" type="password">
      <input id="b" type="text" autocomplete="current-password">
      <input id="c" type="text" autocomplete="new-password">
      <input id="d" type="text">
    `);
    expect(isPasswordField(byId('a'))).toBe(true);
    expect(isPasswordField(byId('b'))).toBe(true);
    expect(isPasswordField(byId('c'))).toBe(true);
    expect(isPasswordField(byId('d'))).toBe(false);
  });
});

describe('isSearchForm', () => {
  const form = () => byId<HTMLFormElement>('f');

  it('is true for role=search on the form', () => {
    mount('<form id="f" role="search"><input name="nombre"><input name="otro"></form>');
    expect(isSearchForm(form())).toBe(true);
  });

  it('is true for a form inside a role=search container', () => {
    mount('<div role="search"><form id="f"><input name="nombre"><input name="otro"></form></div>');
    expect(isSearchForm(form())).toBe(true);
  });

  it('is true for a single field named q', () => {
    mount('<form id="f"><input type="text" name="q"><button>Buscar</button></form>');
    expect(isSearchForm(form())).toBe(true);
  });

  it('is true for a single field with a search placeholder', () => {
    mount('<form id="f"><input type="text" placeholder="Buscar recetas"><button>Ir</button></form>');
    expect(isSearchForm(form())).toBe(true);
  });

  it('is true for a single type=search field', () => {
    mount('<form id="f"><input type="search" name="termino"></form>');
    expect(isSearchForm(form())).toBe(true);
  });

  it('is false for a single field that is not a search field', () => {
    mount('<form id="f"><input type="text" name="nombre"><button>Enviar</button></form>');
    expect(isSearchForm(form())).toBe(false);
  });

  it('is false for a single personal-data field even if the hint mentions search', () => {
    mount('<form id="f"><input type="text" placeholder="Buscar por correo"><button>Ir</button></form>');
    expect(isSearchForm(form())).toBe(false);
  });

  it('is false when there is more than one text field', () => {
    mount('<form id="f"><input type="text" name="q"><input type="text" name="nombre"></form>');
    expect(isSearchForm(form())).toBe(false);
  });

  it('is false when the form has a password field', () => {
    mount('<form id="f"><input type="text" name="q"><input type="password" name="clave"></form>');
    expect(isSearchForm(form())).toBe(false);
  });

  it('is false for a form with no text fields', () => {
    mount('<form id="f"><input type="hidden" name="q" value="x"><button>Ir</button></form>');
    expect(isSearchForm(form())).toBe(false);
  });

  it('counts hidden-by-style fields as absent', () => {
    mount('<form id="f"><input type="text" name="q"><input type="text" name="extra" style="display:none"></form>');
    expect(isSearchForm(form())).toBe(true);
  });

  it('ignores checkboxes and selects when counting text fields', () => {
    mount('<form id="f"><input type="text" name="q"><input type="checkbox" name="exacto"><select name="orden"><option>a</option></select></form>');
    expect(isSearchForm(form())).toBe(true);
  });
});

describe('fieldPiiKind', () => {
  function kindOf(html: string) {
    mount(html);
    const el = byId('x');
    return fieldPiiKind(el, labelOf(el));
  }

  it('flags email and tel inputs as pii', () => {
    expect(kindOf('<label for="x">Correo</label><input id="x" type="email">')).toEqual({ pii: true, payment: false });
    expect(kindOf('<label for="x">Dato</label><input id="x" type="tel">')).toEqual({ pii: true, payment: false });
  });

  it('flags personal-data labels on plain text inputs', () => {
    expect(kindOf('<label for="x">CURP</label><input id="x" type="text" name="curp">').pii).toBe(true);
    expect(kindOf('<label for="x">RFC</label><input id="x" type="text">').pii).toBe(true);
    expect(kindOf('<label for="x">Número de seguridad social</label><input id="x" type="text">').pii).toBe(true);
    expect(kindOf('<label for="x">Teléfono</label><input id="x" type="text">').pii).toBe(true);
    expect(kindOf('<label for="x">Celular</label><input id="x" type="text">').pii).toBe(true);
    expect(kindOf('<label for="x">WhatsApp</label><input id="x" type="text">').pii).toBe(true);
    expect(kindOf('<label for="x">Correo electrónico</label><input id="x" type="text">').pii).toBe(true);
  });

  it('uses attributes when there is no helpful label', () => {
    expect(kindOf('<input id="x" type="text" name="telefono">').pii).toBe(true);
    expect(kindOf('<input id="x" type="text" autocomplete="email">').pii).toBe(true);
    expect(kindOf('<input id="x" type="text" autocomplete="tel-national">').pii).toBe(true);
  });

  it('does not flag ordinary fields', () => {
    expect(kindOf('<label for="x">Nombre</label><input id="x" type="text" name="nombre">')).toEqual({ pii: false, payment: false });
    expect(kindOf('<label for="x">Comentarios</label><textarea id="x"></textarea>')).toEqual({ pii: false, payment: false });
    expect(kindOf('<label for="x">Hotel</label><input id="x" type="text">')).toEqual({ pii: false, payment: false });
  });

  it('flags payment fields and marks them as pii too', () => {
    expect(kindOf('<input id="x" type="text" autocomplete="cc-number">')).toEqual({ pii: true, payment: true });
    expect(kindOf('<label for="x">Número de tarjeta</label><input id="x" type="text">')).toEqual({ pii: true, payment: true });
    expect(kindOf('<label for="x">CVV</label><input id="x" type="text">')).toEqual({ pii: true, payment: true });
    expect(kindOf('<label for="x">Vencimiento</label><input id="x" type="text">')).toEqual({ pii: true, payment: true });
  });

  it('never flags a search field', () => {
    expect(kindOf('<label for="x">Buscar</label><input id="x" type="search" name="q">')).toEqual({ pii: false, payment: false });
    expect(kindOf('<div role="search"><label for="x">Correo</label><input id="x" type="text"></div>')).toEqual({ pii: false, payment: false });
  });
});

describe('flagsFor', () => {
  it('marks a search button as submit and search_form but not consequential', () => {
    mount('<form role="search"><input name="q" placeholder="Buscar"><button id="x">Buscar</button></form>');
    const flags = flagsFor(byId('x'), 'Buscar');
    expect(flags).toContain('submit');
    expect(flags).toContain('search_form');
    expect(flags).not.toContain('consequential');
  });

  it('marks a contact-form submit as submit and consequential', () => {
    mount('<form id="contact"><input name="nombre"><input name="curp"><button id="x">Enviar solicitud</button></form>');
    const flags = flagsFor(byId('x'), 'Enviar solicitud');
    expect(flags).toContain('submit');
    expect(flags).toContain('consequential');
    expect(flags).not.toContain('search_form');
  });

  it('marks pii, password, payment, captcha and disabled', () => {
    mount(`
      <input id="mail" type="email">
      <input id="pass" type="password">
      <input id="card" type="text" autocomplete="cc-number">
      <button id="pay" type="button">Pagar</button>
      <div class="g-recaptcha"><button id="cap" type="button">Soy humano</button></div>
      <button id="off" type="button" disabled>Nada</button>
    `);
    expect(flagsFor(byId('mail'), 'Correo')).toContain('pii');
    expect(flagsFor(byId('pass'), 'Contraseña')).toContain('password');
    expect(flagsFor(byId('card'), 'Tarjeta')).toEqual(expect.arrayContaining(['pii', 'payment']));
    expect(flagsFor(byId('pay'), 'Pagar')).toEqual(expect.arrayContaining(['payment', 'consequential']));
    expect(flagsFor(byId('cap'), 'Soy humano')).toContain('captcha');
    expect(flagsFor(byId('off'), 'Nada')).toContain('disabled');
  });

  it('does not mark a login button as consequential', () => {
    mount('<form><input type="text" name="usuario"><input type="password" name="clave"><button id="x">Iniciar sesión</button></form>');
    const flags = flagsFor(byId('x'), 'Iniciar sesión');
    expect(flags).toContain('submit');
    expect(flags).not.toContain('consequential');
  });
});

describe('gate reasons', () => {
  it('does not gate the submit of a search-only form', () => {
    mount('<form role="search"><input name="q"><button id="x">Buscar</button></form>');
    expect(clickGateReasons(byId('x'))).toEqual([]);
  });

  it('gates a real form submit and a consequential click', () => {
    mount('<form><input name="nombre"><button id="x">Enviar solicitud</button></form><button id="y" type="button">Pagar ahora</button>');
    expect(clickGateReasons(byId('x'))).toEqual(expect.arrayContaining(['submit_form', 'consequential']));
    expect(clickGateReasons(byId('y'))).toEqual(expect.arrayContaining(['consequential', 'payment']));
  });

  it('gates typing into pii fields and typing pii values', () => {
    mount('<input id="mail" type="email"><input id="nombre" type="text" name="nombre"><input id="q" type="search">');
    expect(typeGateReasons(byId('mail'), 'hola')).toContain('pii_field');
    expect(typeGateReasons(byId('nombre'), 'GOMA800101HJCRRN09')).toEqual(['pii_value']);
    expect(typeGateReasons(byId('nombre'), 'Ana')).toEqual([]);
    expect(typeGateReasons(byId('q'), 'recetas de pozole')).toEqual([]);
  });

  it('gates Enter in a non-search form field and in an editor', () => {
    mount(`
      <form><input id="a" type="text" name="nombre"></form>
      <form role="search"><input id="b" type="text" name="q"></form>
      <textarea id="c"></textarea>
    `);
    expect(enterGateReasons(byId('a'))).toEqual(['enter_submits_form']);
    expect(enterGateReasons(byId('b'))).toEqual([]);
    expect(enterGateReasons(byId('c'))).toEqual(['enter_in_editor']);
    expect(enterGateReasons(null)).toEqual([]);
  });
});
