/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Save,
    X,
    Loader,
    AlertCircle,
    ArrowDown,
    Shield,
    HelpCircle,
} from 'lucide-react';
import { ResourceGroup, SkillGlobalGovernance } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { governanceRelaxations } from '../../../lib/governanceInheritance';
import GovernanceRelaxationNotice from '../limits/GovernanceRelaxationNotice';
import InfoBanner from '../../common/InfoBanner';
import { GroupSkillGovernanceHelpModal } from './GroupSkillGovernanceHelpModal';
import SkillGovernanceForm from './SkillGovernanceForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { governanceApprovalTimeoutProblem } from '../../../lib/approvalTimeout';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupSkillGovernanceTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { getSkillGlobalGovernance, updateSkillGlobalGovernance } = useGateway();

    const [governance, setGovernance] = useState<SkillGlobalGovernance>({});
    const [inheritedGovernance, setInheritedGovernance] = useState<SkillGlobalGovernance | null>(null);
    const [effectiveGovernance, setEffectiveGovernance] = useState<SkillGlobalGovernance | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);
    const [hasChanges, setHasChanges] = useState(false);
    const [helpOpen, setHelpOpen] = useState(false);
    const [helpTab, setHelpTab] = useState<'execution' | 'discovery' | 'approval'>('execution');

    const hasParent = !!group.parent_group_id;
    // Fields set looser than the inherited policy. The API refuses these
    // with a 400, so Save stays off until they are resolved.
    const relaxations = governanceRelaxations(governance, inheritedGovernance, 'skill');

    // Load governance
    useEffect(() => {
        runBackground(loadGovernance(), 'governance');
    }, [group.id]);

    const loadGovernance = async () => {
        try {
            setLoading(true);
            setError('');
            const data = await getSkillGlobalGovernance(group.id);
            setGovernance(data.skill_global_governance || {});
            setInheritedGovernance(data.inherited_skill_governance || null);
            setEffectiveGovernance(data.effective_skill_governance || null);
            setHasChanges(false);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load skill governance'));
        } finally {
            setLoading(false);
        }
    };

    // Apply an edit from the presentational form
    const handleChange = (next: SkillGlobalGovernance | Record<string, any>) => {
        setGovernance(next as SkillGlobalGovernance);
        setHasChanges(true);
    };

    // Handle save
    // An approval timeout the Control Plane refuses is not sent: Save stays off while there is one.
    const timeoutProblem = governanceApprovalTimeoutProblem(governance);

    const handleSave = async () => {
        if (timeoutProblem !== null) return;
        try {
            setSaving(true);
            setError('');
            const saved = await updateSkillGlobalGovernance(group.id, governance);
            // The effective policy just changed; keep what the form shows true.
            if (saved) {
                setInheritedGovernance(saved.inherited_skill_governance || null);
                setEffectiveGovernance(saved.effective_skill_governance || null);
            }
            setHasChanges(false);
            onUpdate();
        } catch (err) {
            setError(errorMessage(err, 'Failed to update skill governance'));
        } finally {
            setSaving(false);
        }
    };

    // Handle cancel
    const handleCancel = () => {
        runBackground(loadGovernance(), 'governance');
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center p-12">
                <Loader className="w-8 h-8 animate-spin text-primary" />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div>
                        <h2 className="text-lg font-semibold text-default">Skill Global Governance</h2>
                        <p className="text-sm text-secondary mt-1">
                            Configure global governance policies for agent skill execution across this group.
                        </p>
                    </div>
                    <button
                        onClick={() => { setHelpTab('execution'); setHelpOpen(true); }}
                        className="text-muted hover:text-primary transition-colors"
                        title="Help"
                    >
                        <HelpCircle className="w-5 h-5" />
                    </button>
                </div>
                {hasChanges && (
                    <div className="flex gap-2">
                        <button
                            onClick={handleCancel}
                            disabled={saving}
                            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt"
                        >
                            <X className="w-4 h-4" />
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleSave, 'click')}
                            disabled={saving || relaxations.length > 0 || timeoutProblem !== null}
                            title={
                                timeoutProblem ??
                                (relaxations.length > 0 ? 'Some fields are looser than the inherited policy' : undefined)
                            }
                            data-testid="governance-save"
                            className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {saving ? (
                                <>
                                    <Loader className="w-4 h-4 animate-spin" />
                                    Saving...
                                </>
                            ) : (
                                <>
                                    <Save className="w-4 h-4" />
                                    Save Changes
                                </>
                            )}
                        </button>
                    </div>
                )}
            </div>

            {/* Error */}
            {error && (
                <div className="p-4 bg-surface-alt border border-default rounded-lg flex items-start gap-3">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-red-800">{error}</p>
                </div>
            )}

            {/* Info */}
            <InfoBanner
                icon={<Shield className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />}
            >
                <p>
                    Skill governance uses <strong>governance inheritance</strong> — the effective policy is always the most
                    restrictive value across this group and all its ancestors.
                </p>
                {hasParent && inheritedGovernance && (
                    <p className="mt-1">
                        Fields marked with <ArrowDown className="w-3 h-3 inline" /> have inherited values from parent groups.
                    </p>
                )}
            </InfoBanner>

            <GovernanceRelaxationNotice relaxations={relaxations} />

            {/* Governance Card */}
            <SkillGovernanceForm
                value={governance}
                onChange={handleChange}
                inherited={inheritedGovernance}
                effective={effectiveGovernance}
            />

            <GroupSkillGovernanceHelpModal
                isOpen={helpOpen}
                onClose={() => setHelpOpen(false)}
                initialTab={helpTab}
            />
        </div>
    );
};

export default GroupSkillGovernanceTab;
