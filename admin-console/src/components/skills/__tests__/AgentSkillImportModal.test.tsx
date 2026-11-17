/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Bulk skill import from zip archives.
 *
 * The filtering is the load-bearing part. "Select Directory" hands the input
 * every file in the tree — READMEs, .DS_Store, loose scripts — and the backend
 * would reject each one individually, producing a wall of errors for files the
 * operator never meant to upload. So non-zip entries are dropped before they
 * are ever listed, and a file already in the list is not added twice (picking
 * the same archive from two folders would otherwise import it twice).
 *
 * The other half is partial failure: a batch where some archives import and
 * others do not is the normal case, and it must not be reported as either a
 * clean success or a total failure.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

import AgentSkillImportModal from '../AgentSkillImportModal';
import type { AgentSkillImportResponse, AgentSkillLimits } from '../../../lib/agentSkillTypes';

const zip = (name: string, size = 2048) =>
    new File(['x'.repeat(size)], name, { type: 'application/zip' });

function setup(
    onImport: () => Promise<AgentSkillImportResponse> = vi.fn(),
    limits: AgentSkillLimits | null = null,
) {
    const onClose = vi.fn();
    const onImportComplete = vi.fn();
    render(
        <AgentSkillImportModal
            onClose={onClose}
            onImport={onImport}
            onImportComplete={onImportComplete}
            limits={limits}
        />
    );
    return { onClose, onImportComplete };
}

const dropFiles = (files: File[]) => {
    const zone = screen.getByText('Drag & drop skill zip files here').parentElement!;
    fireEvent.drop(zone, { dataTransfer: { files } });
    return zone;
};

const result = (over: Record<string, unknown> = {}) => ({
    zip_filename: 'alpha.zip',
    folder: '',
    status: 'imported',
    success: true,
    skill_name: 'invoice-extractor',
    skill_id: 's-1',
    error: null,
    reason: null,
    resources_count: 0,
    scripts_count: 0,
    skipped_files: [],
    ...over,
});

/** A whole answer of the import: its counts follow from `results`. */
const answer = (results: ReturnType<typeof result>[], archives: { zip_filename: string; error: string | null; unassigned_files: string[] }[] = []) => {
    const count = (status: string) => results.filter((r) => r.status === status).length;
    return {
        success: count('imported') > 0,
        message: `Imported ${count('imported')} skill(s)`,
        total_files: new Set(results.map((r) => r.zip_filename)).size,
        success_count: count('imported'),
        error_count: count('refused') + count('skipped'),
        refused_count: count('refused'),
        skipped_count: count('skipped'),
        results,
        archives,
        limits: LIMITS,
    };
};

const LIMITS: AgentSkillLimits = { skill_max_bytes: 30_000_000, archive_max_bytes: 300_000_000, archive_max_skills: 50 };

describe('AgentSkillImportModal — choosing files', () => {
    it('starts with nothing selected and nothing importable', () => {
        setup();
        expect(screen.getByRole('button', { name: /Import 0 Skills/ })).toBeDisabled();
    });

    it('accepts dropped zip archives', () => {
        setup();
        dropFiles([zip('alpha.zip'), zip('beta.zip')]);

        expect(screen.getByText('2 files selected')).toBeInTheDocument();
        expect(screen.getByText('alpha.zip')).toBeInTheDocument();
        expect(
            screen.getByRole('button', { name: /Import 2 Skills/ })
        ).not.toBeDisabled();
    });

    it('silently drops non-zip files a directory pick sweeps up', () => {
        // Listing README.md only to have the backend reject it is noise.
        setup();
        dropFiles([zip('alpha.zip'), new File(['#'], 'README.md')]);

        expect(screen.getByText('1 file selected')).toBeInTheDocument();
        expect(screen.queryByText('README.md')).not.toBeInTheDocument();
    });

    it('selects nothing at all when the drop contained no archives', () => {
        setup();
        dropFiles([new File(['#'], 'notes.txt')]);
        expect(screen.queryByText(/file(s)? selected/)).not.toBeInTheDocument();
    });

    it('does not queue the same archive twice', () => {
        setup();
        dropFiles([zip('alpha.zip')]);
        dropFiles([zip('alpha.zip'), zip('gamma.zip')]);

        expect(screen.getByText('2 files selected')).toBeInTheDocument();
        expect(screen.getAllByText('alpha.zip')).toHaveLength(1);
    });

    it('shows each archive size in human units', () => {
        setup();
        dropFiles([zip('alpha.zip', 2048)]);
        expect(screen.getByText('2.0 KB')).toBeInTheDocument();
    });

    it('drops one archive without disturbing the rest', () => {
        setup();
        dropFiles([zip('alpha.zip'), zip('beta.zip')]);

        const alphaRow = screen.getByText('alpha.zip').closest('.group') as HTMLElement;
        fireEvent.click(alphaRow.querySelector('button') as HTMLElement);

        expect(screen.getByText('1 file selected')).toBeInTheDocument();
        expect(screen.getByText('beta.zip')).toBeInTheDocument();
        expect(screen.queryByText('alpha.zip')).not.toBeInTheDocument();
    });

    it('clears the whole queue', () => {
        setup();
        dropFiles([zip('alpha.zip'), zip('beta.zip')]);
        fireEvent.click(screen.getByText('Clear all'));

        expect(screen.queryByText(/files selected/)).not.toBeInTheDocument();
        expect(screen.getByRole('button', { name: /Import 0 Skills/ })).toBeDisabled();
    });

    it('says what a valid archive must contain', () => {
        setup();
        expect(screen.getByText('SKILL.md')).toBeInTheDocument();
        expect(screen.getByText(/imported as/)).toHaveTextContent(
            /Draft status and assigned to the Default Resource Group/
        );
    });
});

describe('AgentSkillImportModal — importing', () => {
    it('sends every queued archive in one call', async () => {
        const onImport = vi.fn().mockResolvedValue({
            success: true,
            message: 'Imported 2 skills',
            total_files: 2,
            success_count: 2,
            error_count: 0,
            results: [result(), result({ skill_name: 'beta' })],
        });
        setup(onImport);
        dropFiles([zip('alpha.zip'), zip('beta.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 2 Skills/ }));

        await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
        expect((onImport.mock.calls[0][0] as File[]).map((f) => f.name)).toEqual([
            'alpha.zip',
            'beta.zip',
        ]);
    });

    it('reports a clean batch and refreshes the parent list', async () => {
        const { onImportComplete } = setup(
            vi.fn().mockResolvedValue({
                success: true,
                message: 'Imported 1 skill',
                total_files: 1,
                success_count: 1,
                error_count: 0,
                results: [result({ resources_count: 3, scripts_count: 2 })],
            })
        );
        dropFiles([zip('alpha.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 1 Skill/ }));

        expect(await screen.findByText('Imported 1 skill')).toBeInTheDocument();
        expect(screen.getByText('1 file processed')).toBeInTheDocument();
        expect(screen.getByText('3 resources')).toBeInTheDocument();
        expect(screen.getByText('2 scripts')).toBeInTheDocument();
        expect(onImportComplete).toHaveBeenCalled();
    });

    it('says "SKILL.md only" rather than showing two zero counts', async () => {
        setup(
            vi.fn().mockResolvedValue({
                success: true,
                message: 'Imported 1 skill',
                total_files: 1,
                success_count: 1,
                error_count: 0,
                results: [result()],
            })
        );
        dropFiles([zip('alpha.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 1 Skill/ }));

        expect(await screen.findByText('SKILL.md only')).toBeInTheDocument();
    });

    it('reports a partially-failed batch per archive', async () => {
        // The common real case: one bad zip in a folder of good ones.
        const { onImportComplete } = setup(
            vi.fn().mockResolvedValue({
                success: false,
                message: 'Imported 1 of 2 skills',
                total_files: 2,
                success_count: 1,
                error_count: 1,
                results: [
                    result(),
                    result({
                        status: 'refused',
                        reason: 'no_skill',
                        success: false,
                        skill_name: 'broken.zip',
                        error: 'SKILL.md not found in archive root',
                    }),
                ],
            })
        );
        dropFiles([zip('alpha.zip'), zip('broken.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 2 Skills/ }));

        expect(await screen.findByText('Imported 1 of 2 skills')).toBeInTheDocument();
        expect(
            screen.getByText('SKILL.md not found in archive root')
        ).toBeInTheDocument();
        // One skill did land, so the list behind the modal is stale.
        expect(onImportComplete).toHaveBeenCalled();
    });

    it('does not refresh the list when nothing was imported', async () => {
        const { onImportComplete } = setup(
            vi.fn().mockResolvedValue({
                success: false,
                message: 'Nothing imported',
                total_files: 1,
                success_count: 0,
                error_count: 1,
                results: [result({ status: 'refused', reason: 'invalid_archive', success: false, error: 'bad zip' })],
            })
        );
        dropFiles([zip('alpha.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 1 Skill/ }));

        await screen.findByText('Nothing imported');
        expect(onImportComplete).not.toHaveBeenCalled();
    });

    it('turns a transport failure into a per-file failure report', async () => {
        // Without this the operator sees a modal that simply stops responding.
        setup(vi.fn().mockRejectedValue(new Error('413 Payload Too Large')));
        dropFiles([zip('alpha.zip'), zip('beta.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 2 Skills/ }));

        // Once in the summary, once against each archive that never landed.
        expect(await screen.findAllByText('413 Payload Too Large')).toHaveLength(3);
        expect(screen.getByText('2 files processed')).toBeInTheDocument();
    });

    it('replaces the picker with a Done button once results are in', async () => {
        const { onClose } = setup(
            vi.fn().mockResolvedValue({
                success: true,
                message: 'Imported 1 skill',
                total_files: 1,
                success_count: 1,
                error_count: 0,
                results: [result()],
            })
        );
        dropFiles([zip('alpha.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 1 Skill/ }));

        const done = await screen.findByRole('button', { name: 'Done' });
        expect(
            screen.queryByText('Drag & drop skill zip files here')
        ).not.toBeInTheDocument();

        fireEvent.click(done);
        expect(onClose).toHaveBeenCalled();
    });

    it('cancels without importing anything', () => {
        const onImport = vi.fn();
        const { onClose } = setup(onImport);
        dropFiles([zip('alpha.zip')]);
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

        expect(onClose).toHaveBeenCalled();
        expect(onImport).not.toHaveBeenCalled();
    });
});

describe('AgentSkillImportModal — the limits', () => {
    it('states the limits an import is held to, in MB', () => {
        setup(vi.fn(), LIMITS);
        expect(screen.getByTestId('skill-import-limits')).toHaveTextContent(
            'A skill may hold up to 30 MB of files. A zip may hold up to 50 skills and 300 MB.'
        );
    });

    it('says nothing about limits it was not given', () => {
        setup();
        expect(screen.queryByTestId('skill-import-limits')).toBeNull();
    });

    it('says a zip may hold several skills, one per top-level folder', () => {
        setup(vi.fn(), LIMITS);
        expect(screen.getByText(/one skill per top-level folder/)).toBeInTheDocument();
    });
});

describe('AgentSkillImportModal — the result per skill', () => {
    async function importWith(response: ReturnType<typeof answer>) {
        const { onImportComplete } = setup(vi.fn().mockResolvedValue(response), LIMITS);
        dropFiles([zip('bundle.zip')]);
        fireEvent.click(screen.getByRole('button', { name: /Import 1 Skill/ }));
        await screen.findByRole('button', { name: 'Done' });
        return { onImportComplete };
    }

    const row = (name: string) => screen.getByText(name).closest('[data-testid="skill-import-result"]') as HTMLElement;

    it('lists each skill of a zip with its folder and what became of it', async () => {
        await importWith(
            answer([
                result({ zip_filename: 'bundle.zip', folder: 'pdf', skill_name: 'pdf', resources_count: 2 }),
                result({
                    zip_filename: 'bundle.zip', folder: 'huge', skill_name: 'huge', status: 'refused', success: false,
                    skill_id: null, reason: 'too_large', error: 'The skill unpacks to more than 30 MB',
                }),
                result({
                    zip_filename: 'bundle.zip', folder: 'docx', skill_name: 'docx', status: 'skipped', success: false,
                    skill_id: null, reason: 'already_exists', error: "Skill 'docx' already exists",
                }),
            ], [{ zip_filename: 'bundle.zip', error: null, unassigned_files: [] }]),
        );

        expect(within(row('pdf')).getByText('Imported')).toBeInTheDocument();
        expect(within(row('pdf')).getByText('bundle.zip / pdf')).toBeInTheDocument();
        expect(within(row('pdf')).getByText('2 resources')).toBeInTheDocument();

        expect(within(row('huge')).getByText('Refused')).toBeInTheDocument();
        expect(within(row('huge')).getByText('The skill is larger than the size limit')).toBeInTheDocument();
        expect(within(row('huge')).getByText('The skill unpacks to more than 30 MB')).toBeInTheDocument();

        expect(within(row('docx')).getByText('Skipped')).toBeInTheDocument();
        expect(within(row('docx')).getByText('A skill of this name already exists')).toBeInTheDocument();
    });

    it('sums up imported, refused and skipped apart', async () => {
        await importWith(
            answer([
                result({ zip_filename: 'bundle.zip', folder: 'a', skill_name: 'a' }),
                result({ zip_filename: 'bundle.zip', folder: 'b', skill_name: 'b', status: 'refused', success: false, reason: 'invalid_skill', error: 'bad front matter' }),
                result({ zip_filename: 'bundle.zip', folder: 'c', skill_name: 'c', status: 'skipped', success: false, reason: 'already_exists', error: 'exists' }),
            ]),
        );
        expect(screen.getByTestId('skill-import-counts')).toHaveTextContent('1 imported · 1 refused · 1 skipped');
    });

    it('names a reason it does not know by its code', async () => {
        await importWith(
            answer([result({ folder: null, skill_name: 'bundle.zip', zip_filename: 'bundle.zip', status: 'refused', success: false, reason: 'something_new', error: 'no' })]),
        );
        expect(within(row('bundle.zip')).getByText('something_new')).toBeInTheDocument();
    });

    it('lists the files of each zip that belong to no skill, and says they were not imported', async () => {
        await importWith(
            answer(
                [result({ zip_filename: 'bundle.zip', folder: 'pdf', skill_name: 'pdf' })],
                [{ zip_filename: 'bundle.zip', error: null, unassigned_files: ['README.md', 'notes/todo.txt'] }],
            ),
        );
        const block = screen.getByTestId('skill-import-unassigned');
        expect(block).toHaveTextContent('bundle.zip: 2 files belong to no skill and were not imported');
        expect(within(block).getByText('README.md')).toBeInTheDocument();
        expect(within(block).getByText('notes/todo.txt')).toBeInTheDocument();
    });

    it('shows no unassigned block when every file belongs to a skill', async () => {
        await importWith(answer([result({ zip_filename: 'bundle.zip' })], [{ zip_filename: 'bundle.zip', error: null, unassigned_files: [] }]));
        expect(screen.queryByTestId('skill-import-unassigned')).toBeNull();
    });
});

