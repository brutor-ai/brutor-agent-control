/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The operand of an equality or membership rule of an argument policy
 * (`field_eq`, `field_ne`, `field_in`, `field_not_in`).
 *
 * The gateway compares a field with an operand of the field's own type: a
 * string, a number or a boolean. A field that holds another type than the
 * operand cannot be decided, and the call is refused. Text typed into a
 * form is always text, so the form asks which of the three is meant
 * (`OperandKind`) and the operand is sent as that. The threshold of an
 * ordering rule (`field_gt` and the like) is always a number and is not
 * handled here.
 */

export type OperandKind = 'text' | 'number' | 'boolean';

export const OPERAND_KIND_LABEL: Record<OperandKind, string> = {
    text: 'Text',
    number: 'Number',
    boolean: 'True or false',
};

export type Converted<T> = { ok: true; value: T } | { ok: false; error: string };

/** What is typed (or stored), as an operand of `kind`. */
export function toOperand(kind: OperandKind, raw: unknown): Converted<string | number | boolean> {
    // A stored operand is a string, a number or a boolean; anything else is no operand.
    const typed = typeof raw === 'string' ? raw : typeof raw === 'number' || typeof raw === 'boolean' ? String(raw) : '';
    if (kind === 'text') return { ok: true, value: typed };
    const text = typed.trim();
    if (kind === 'number') {
        const number = text === '' ? Number.NaN : Number(text);
        return Number.isFinite(number) ? { ok: true, value: number } : { ok: false, error: `"${typed}" is not a number` };
    }
    const word = text.toLowerCase();
    if (word === 'true') return { ok: true, value: true };
    if (word === 'false') return { ok: true, value: false };
    return { ok: false, error: `"${typed}" is not true or false` };
}

/**
 * How the options of a membership rule are compared: as one of the three
 * kinds, or `mixed` for a stored list that holds more than one type. A
 * mixed list is not text: read as text, `[100, "EUR"]` would be saved as
 * `["100", "EUR"]` and the number would no longer match. The form opens
 * such a list as it is stored and keeps each value's type, until the
 * administrator chooses one kind for all of them.
 */
export type ListOperandKind = OperandKind | 'mixed';

export const MIXED_KIND_LABEL = 'As stored (more than one type)';

const NOT_A_STORED_VALUE = 'is not text in double quotes, a number, or true or false';

/** One line of a mixed list, as the value it stands for. */
function fromStoredLine(line: string): Converted<string | number | boolean> {
    let parsed: unknown;
    try {
        parsed = JSON.parse(line);
    } catch {
        parsed = undefined;
    }
    if (typeof parsed === 'string' || typeof parsed === 'boolean' || (typeof parsed === 'number' && Number.isFinite(parsed))) {
        return { ok: true, value: parsed };
    }
    return { ok: false, error: `"${line}" ${NOT_A_STORED_VALUE}` };
}

/**
 * The options of a membership rule, each as an operand of `kind`. Blank
 * lines are no option. Of a mixed list each line is one value as it is
 * stored (`listLines`), and keeps its own type.
 */
export function toOperandList(kind: ListOperandKind, raws: readonly unknown[]): Converted<Array<string | number | boolean>> {
    const options: Array<string | number | boolean> = [];
    for (const raw of raws) {
        const text = typeof raw === 'string' ? raw.trim() : raw;
        if (text === '' || text === undefined || text === null) continue;
        const converted =
            kind !== 'mixed' ? toOperand(kind, text)
            : typeof text === 'string' ? fromStoredLine(text)
            : typeof text === 'number' || typeof text === 'boolean' ? { ok: true as const, value: text }
            : { ok: false as const, error: `"${JSON.stringify(text)}" ${NOT_A_STORED_VALUE}` };
        if (!converted.ok) return converted;
        options.push(converted.value);
    }
    return { ok: true, value: options };
}

/**
 * The lines the options of a list are edited in. Of one kind, each value
 * plainly (`EUR`, `100`, `true`). Of a mixed list, each value as it is
 * stored, so that text is told from a number or a boolean: `"EUR"`, `100`,
 * `true`, `"100"`.
 */
export function listLines(kind: ListOperandKind, values: readonly unknown[] | null | undefined): string[] {
    return (values ?? []).map((value) => (kind === 'mixed' ? JSON.stringify(value) ?? '' : String(value)));
}

/** The kind a stored operand is. */
export function operandKindOf(value: unknown): OperandKind {
    if (typeof value === 'boolean') return 'boolean';
    if (typeof value === 'number') return 'number';
    return 'text';
}

/** The kind the options of a stored membership rule are: the one they all share, else mixed. */
export function operandListKindOf(values: readonly unknown[] | null | undefined): ListOperandKind {
    if (!values || values.length === 0) return 'text';
    const kinds = new Set(values.map((value) => typeof value));
    return kinds.size === 1 ? operandKindOf(values[0]) : 'mixed';
}
