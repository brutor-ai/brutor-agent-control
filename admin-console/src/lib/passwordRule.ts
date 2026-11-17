/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The password rule, as the Control Plane states it: a password has at
 * least 8 characters and is not blank (not only spaces, tabs or line ends).
 *
 * Every form that sets a password applies it before the request, so a
 * password the Control Plane refuses is refused where it is typed, in the
 * same words. A sign-in form does not apply it: it sends what it is given.
 */

export const MIN_PASSWORD_LENGTH = 8;

/** The rule in one sentence. */
export const PASSWORD_RULE = `A password has at least ${MIN_PASSWORD_LENGTH} characters and is not blank.`;

/** What a form says of a password that does not meet the rule, after the name it gives the field. */
export const PASSWORD_RULE_HINT = `at least ${MIN_PASSWORD_LENGTH} characters and not blank`;

/** True when `password` meets the rule. Characters are counted as the Control Plane counts them (code points). */
export function passwordMeetsRule(password: string): boolean {
    return [...password].length >= MIN_PASSWORD_LENGTH && password.trim() !== '';
}
