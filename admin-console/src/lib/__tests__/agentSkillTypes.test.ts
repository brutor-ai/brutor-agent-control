/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Skill identity helpers. The name pattern mirrors the control plane's
 * `_validate_skill_name`, so a name the console accepts must be one the API
 * accepts — the two drifting apart is a save that fails after the operator
 * has finished typing.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
    SKILL_NAME_PATTERN,
    downloadBlobAsFile,
    formatSkillName,
    getSkillCategory,
    getSkillIconName,
    getSkillTags,
    isInDefaultGroup,
    isValidSkillName,
    type AgentSkillCatalogEntry,
    type AgentSkillConfig,
} from '../agentSkillTypes';
import { installBlobDownloadSpies, type CapturedDownload } from './helpers/blobDownload';

const skill = (over: Partial<AgentSkillConfig> = {}): AgentSkillConfig =>
    ({ id: 's1', name: 'query-sales-data', ...over }) as AgentSkillConfig;

const entry = (over: Partial<AgentSkillCatalogEntry> = {}): AgentSkillCatalogEntry =>
    ({ id: 'c1', name: 'query-sales-data', ...over }) as AgentSkillCatalogEntry;

describe('isValidSkillName', () => {
    it('accepts lowercase hyphen-case names', () => {
        expect(isValidSkillName('query-sales-data')).toBe(true);
        expect(isValidSkillName('ab')).toBe(true);
        expect(isValidSkillName('a1')).toBe(true);
    });

    it('requires a letter first and a letter or digit last', () => {
        expect(isValidSkillName('1skill')).toBe(false);
        expect(isValidSkillName('-skill')).toBe(false);
        expect(isValidSkillName('skill-')).toBe(false);
    });

    it('rejects uppercase, underscores, spaces and dots', () => {
        expect(isValidSkillName('QuerySales')).toBe(false);
        expect(isValidSkillName('query_sales')).toBe(false);
        expect(isValidSkillName('query sales')).toBe(false);
        expect(isValidSkillName('query.sales')).toBe(false);
    });

    it('rejects a single character and the empty string', () => {
        expect(isValidSkillName('a')).toBe(false);
        expect(isValidSkillName('')).toBe(false);
    });

    it('caps the length at 64 characters', () => {
        expect(isValidSkillName('a'.repeat(64))).toBe(true);
        expect(isValidSkillName('a'.repeat(65))).toBe(false);
    });

    it('anchors both ends — a valid name embedded in junk is not valid', () => {
        expect(isValidSkillName(' query-sales ')).toBe(false);
        expect(isValidSkillName('query-sales\nrm -rf')).toBe(false);
    });

    it('is not a sticky regex, so repeated calls agree', () => {
        expect(SKILL_NAME_PATTERN.flags).not.toContain('g');
        expect(isValidSkillName('ab')).toBe(isValidSkillName('ab'));
    });
});

describe('formatSkillName', () => {
    it('turns hyphen-case into title case', () => {
        expect(formatSkillName('query-sales-data')).toBe('Query Sales Data');
    });

    it('handles a single segment and leaves the rest of a word alone', () => {
        expect(formatSkillName('summarise')).toBe('Summarise');
        expect(formatSkillName('pdf-ocr')).toBe('Pdf Ocr');
    });

    it('does not throw on empty or degenerate input', () => {
        expect(formatSkillName('')).toBe('');
        expect(formatSkillName('a--b')).toBe('A  B');
    });
});

describe('getSkillIconName', () => {
    it('reads the icon from metadata', () => {
        expect(getSkillIconName(skill({ metadata: { icon_name: 'Database' } }))).toBe('Database');
    });

    it('falls back to Sparkles when metadata is absent or empty', () => {
        expect(getSkillIconName(skill())).toBe('Sparkles');
        expect(getSkillIconName(skill({ metadata: {} }))).toBe('Sparkles');
        expect(getSkillIconName(skill({ metadata: { icon_name: '' } }))).toBe('Sparkles');
    });

    it('a catalog entry has no metadata block, so it always falls back', () => {
        expect(getSkillIconName(entry({ icon_name: 'Database' }))).toBe('Sparkles');
    });
});

describe('getSkillCategory', () => {
    it('prefers a catalog entry’s own category column', () => {
        expect(getSkillCategory(entry({ category: 'Finance' }))).toBe('Finance');
    });

    it('falls back to the metadata category on a full config', () => {
        expect(getSkillCategory(skill({ metadata: { category: 'Ops' } }))).toBe('Ops');
    });

    it('is "Other" when nothing names one', () => {
        expect(getSkillCategory(skill())).toBe('Other');
        expect(getSkillCategory(entry())).toBe('Other');
        expect(getSkillCategory(entry({ category: '' }))).toBe('Other');
    });
});

describe('getSkillTags', () => {
    it('prefers the top-level tags list', () => {
        expect(getSkillTags(entry({ tags: ['sql', 'read-only'] }))).toEqual(['sql', 'read-only']);
    });

    it('falls back to metadata tags', () => {
        expect(getSkillTags(skill({ metadata: { tags: ['ops'] } }))).toEqual(['ops']);
    });

    it('returns an empty array rather than undefined when there are none', () => {
        expect(getSkillTags(skill())).toEqual([]);
        expect(getSkillTags(entry())).toEqual([]);
    });

    it('treats an explicitly empty tags list as empty, not as missing', () => {
        expect(getSkillTags(entry({ tags: [] }))).toEqual([]);
    });
});

describe('isInDefaultGroup', () => {
    it('is true when the skill is global', () => {
        expect(isInDefaultGroup(skill({ is_global: true }))).toBe(true);
    });

    it('is true when it is bound to the group named Default', () => {
        expect(
            isInDefaultGroup(skill({ groups: [{ id: 'g1', name: 'Default', is_active: true }] })),
        ).toBe(true);
    });

    it('is false for a skill bound only elsewhere, or bound nowhere', () => {
        expect(
            isInDefaultGroup(skill({ groups: [{ id: 'g2', name: 'Finance', is_active: true }] })),
        ).toBe(false);
        expect(isInDefaultGroup(skill())).toBe(false);
        expect(isInDefaultGroup(skill({ groups: [] }))).toBe(false);
    });

    it('always returns a boolean, never undefined', () => {
        expect(isInDefaultGroup(skill({ is_global: false }))).toBe(false);
    });
});

describe('downloadBlobAsFile', () => {
    let captured: CapturedDownload;

    beforeEach(() => {
        captured = installBlobDownloadSpies();
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('clicks a named link and cleans up after itself', () => {
        const blob = new Blob(['zip']);
        downloadBlobAsFile(blob, 'skill-bundle.zip');
        expect(captured.blobs).toEqual([blob]);
        expect(captured.anchors[0].download).toBe('skill-bundle.zip');
        expect(captured.anchors[0].href).toBe('blob:mock/1');
        expect(captured.clicks).toEqual([1]);
        expect(captured.revoked).toEqual(['blob:mock/1']);
    });

    it('leaves no anchor behind in the document', () => {
        downloadBlobAsFile(new Blob(['zip']), 'skill-bundle.zip');
        expect(document.body.contains(captured.anchors[0])).toBe(false);
    });
});
