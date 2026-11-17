/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The operand of an equality or membership rule of an argument policy.
 *
 * The gateway compares a field with an operand of the field's own type: a
 * string, a number or a boolean. Text typed into a form is always text, so
 * the form asks which of the three is meant and sends the operand as that.
 */

import { describe, expect, it } from 'vitest';

import { listLines, operandKindOf, operandListKindOf, toOperand, toOperandList } from '../argumentPolicyOperands';

describe('toOperand', () => {
    it('sends text as text, whatever it looks like', () => {
        expect(toOperand('text', 'EUR')).toEqual({ ok: true, value: 'EUR' });
        expect(toOperand('text', 'true')).toEqual({ ok: true, value: 'true' });
        expect(toOperand('text', '100')).toEqual({ ok: true, value: '100' });
    });

    it('sends a number as a number, and refuses what is not one', () => {
        expect(toOperand('number', '100')).toEqual({ ok: true, value: 100 });
        expect(toOperand('number', ' 2.5 ')).toEqual({ ok: true, value: 2.5 });
        expect(toOperand('number', 'abc')).toEqual({ ok: false, error: '"abc" is not a number' });
        expect(toOperand('number', '')).toEqual({ ok: false, error: '"" is not a number' });
    });

    it('sends true or false as a boolean, and refuses anything else', () => {
        expect(toOperand('boolean', 'true')).toEqual({ ok: true, value: true });
        expect(toOperand('boolean', ' False ')).toEqual({ ok: true, value: false });
        expect(toOperand('boolean', true)).toEqual({ ok: true, value: true });
        expect(toOperand('boolean', 'yes')).toEqual({ ok: false, error: '"yes" is not true or false' });
    });
});

describe('toOperandList', () => {
    it('converts every option, dropping blank lines', () => {
        expect(toOperandList('boolean', ['true', '', ' false '])).toEqual({ ok: true, value: [true, false] });
        expect(toOperandList('number', ['1', '2'])).toEqual({ ok: true, value: [1, 2] });
        expect(toOperandList('text', [' a ', 'b'])).toEqual({ ok: true, value: ['a', 'b'] });
    });

    it('names the first option that is not of the kind', () => {
        expect(toOperandList('boolean', ['true', 'maybe'])).toEqual({ ok: false, error: '"maybe" is not true or false' });
    });
});

describe('the kind a stored operand is', () => {
    it('is read from its type', () => {
        expect(operandKindOf(true)).toBe('boolean');
        expect(operandKindOf(12)).toBe('number');
        expect(operandKindOf('EUR')).toBe('text');
        expect(operandKindOf(undefined)).toBe('text');
    });

    it('is read from a list when every option is of it, and is text otherwise', () => {
        expect(operandListKindOf([true, false])).toBe('boolean');
        expect(operandListKindOf([1, 2])).toBe('number');
        expect(operandListKindOf(['a', 'b'])).toBe('text');
        // More than one type is not text: the list is opened as it is stored.
        expect(operandListKindOf([1, 'a'])).toBe('mixed');
        expect(operandListKindOf(['true', true])).toBe('mixed');
        expect(operandListKindOf([])).toBe('text');
        expect(operandListKindOf(undefined)).toBe('text');
    });
});

describe('a stored list of more than one type', () => {
    const STORED = ['EUR', 100, true, '100', 'true', 2.5];

    it('is written one value a line, each as it is stored: text in double quotes, a number, true or false', () => {
        expect(listLines('mixed', STORED)).toEqual(['"EUR"', '100', 'true', '"100"', '"true"', '2.5']);
    });

    it('is read back value for value, each with its own type', () => {
        expect(toOperandList('mixed', listLines('mixed', STORED))).toEqual({ ok: true, value: STORED });
    });

    it('takes an edited line in the same notation, and blank lines are no value', () => {
        expect(toOperandList('mixed', ['"EUR"', '', '  7 ', 'false', '"a \\"quoted\\" word"'])).toEqual({ ok: true, value: ['EUR', 7, false, 'a "quoted" word'] });
    });

    it('refuses a line that is none of the three, and says which', () => {
        const error = { ok: false, error: '"EUR" is not text in double quotes, a number, or true or false' };
        expect(toOperandList('mixed', ['"USD"', 'EUR'])).toEqual(error);
        expect(toOperandList('mixed', ['null'])).toEqual({ ok: false, error: '"null" is not text in double quotes, a number, or true or false' });
        expect(toOperandList('mixed', ['[1]'])).toEqual({ ok: false, error: '"[1]" is not text in double quotes, a number, or true or false' });
    });

    it('as one type, is written plainly: the lines a list of that type is typed in', () => {
        expect(listLines('text', STORED)).toEqual(['EUR', '100', 'true', '100', 'true', '2.5']);
        expect(listLines('number', [1, 2])).toEqual(['1', '2']);
        expect(listLines('text', undefined)).toEqual([]);
    });
});
