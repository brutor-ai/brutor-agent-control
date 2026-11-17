/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import toast from 'react-hot-toast';
import {
    X,
    Info,
    FileCode,
    Activity,
    Sparkles,
    Globe,
    CheckCircle,
    AlertTriangle,
    Archive,
    Edit,
    Copy,
    Trash2,
    DollarSign,
    ShieldCheck,
    Gauge,
    Loader2,
    Layers,
    Workflow,
    FolderOpen,
    Code2,
    Package,
    Shield,
    Save,
    ChevronDown,
    ChevronRight,
    Plus,
    FolderTree,
    Terminal,
    Image,
    BookOpen,
    Download,
    Settings,
    Tag,
    FileClock,
} from 'lucide-react';
import {
    AgentSkillConfig,
    AgentSkillResource,
    AgentSkillScript,
    AgentSkillAsset,
    ExecutionSummary,
    getSkillCategory,
    getSkillTags,
    SKILL_STATUS_CONFIG,
    RESOURCE_TYPES,
    SCRIPT_LANGUAGES,
    SCRIPT_EXECUTION_MODES,
    ASSET_TYPES,
    ASSET_ENCODINGS,
    SKILL_CATEGORIES,
    SKILL_LICENSES,
    SKILL_DISCLOSURE_MODES,
    type SkillDisclosureMode,
    type AgentSkillResourceUpdateRequest,
    type AgentSkillScriptUpdateRequest,
    type AgentSkillAssetUpdateRequest,
    isValidSkillName,
} from '../../lib/agentSkillTypes';
import { ValidatedInput, ValidatedTextarea, type ValidationErrorsType as ValidationErrors } from '@/components/llm/config/FormValidation';
import { AgentSkillStatusBadge } from './AgentSkillStatusBadge';
import { ExecutionCard } from './AgentSkillExecutionCard';
import { useGateway } from '../../hooks/useGateway';
import { downloadBlobAsFile } from '../../lib/agentSkillTypes';
import GroupTreeDisplay from '../common/GroupTreeDisplay';
import HierarchicalGroupSelector from '../common/HierarchicalGroupSelector';
import { GroupHierarchyTree } from '../../lib/groupTypes';
// Tenant-wide per-skill Limits/Policies reuse the classic RG skill forms (DRY).
import AuditInfoTab from '../common/AuditInfoTab';
import SkillLimitsForm from '../groups/subtabs/SkillLimitsForm';
import NotInThisRelease from '../common/NotInThisRelease';
import { shownValue } from '../../lib/unavailableSettings';
import { useConfirm } from '../common/ConfirmationDialog';
import { failureSentence, errorKind, errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

type DetailTab = 'overview' | 'basic-info' | 'audit' | 'metadata' | 'groups' | 'content' | 'resources' | 'scripts' | 'assets' | 'limits' | 'executions';

interface AgentSkillDetailPanelProps {
    skill: AgentSkillConfig;
    initialTab?: DetailTab;
    onClose: () => void;
    onCopy: () => void;
    onDelete: () => void;
    onPublish: () => void;
    onValidate: () => void;
    onDeprecate: (reason: string) => void;
    onArchive: () => void;
    /** Callback to load executions (pagination) */
    loadExecutions?: (skillId: string, offset: number, limit: number) => Promise<{ executions: ExecutionSummary[]; total: number }>;
    /** Available standalone skills for workflow skill-node selector */
    /** Callback to load resources for a skill */
    loadResources?: (skillId: string) => Promise<AgentSkillResource[]>;
    /** Callback to load scripts for a skill */
    loadScripts?: (skillId: string) => Promise<AgentSkillScript[]>;
    /** Callback to load assets for a skill */
    loadAssets?: (skillId: string) => Promise<AgentSkillAsset[]>;
    /** CRUD callbacks for resources */
    getResource?: (skillId: string, resourceId: string) => Promise<AgentSkillResource>;
    createResource?: (skillId: string, data: any) => Promise<any>;
    updateResource?: (skillId: string, resourceId: string, data: AgentSkillResourceUpdateRequest) => Promise<unknown>;
    deleteResource?: (skillId: string, resourceId: string) => Promise<any>;
    /** CRUD callbacks for scripts */
    getScript?: (skillId: string, scriptId: string) => Promise<AgentSkillScript>;
    createScript?: (skillId: string, data: any) => Promise<any>;
    updateScript?: (skillId: string, scriptId: string, data: AgentSkillScriptUpdateRequest) => Promise<unknown>;
    deleteScript?: (skillId: string, scriptId: string) => Promise<any>;
    /** CRUD callbacks for assets */
    getAsset?: (skillId: string, assetId: string) => Promise<AgentSkillAsset>;
    createAsset?: (skillId: string, data: any) => Promise<any>;
    updateAsset?: (skillId: string, assetId: string, data: AgentSkillAssetUpdateRequest) => Promise<unknown>;
    deleteAsset?: (skillId: string, assetId: string) => Promise<any>;
    /** Callback to fetch the group hierarchy tree */
    fetchGroupTree?: () => Promise<GroupHierarchyTree[]>;
    /** Callback to update skill group access */
    onUpdateGroups?: (skillId: string, groupIds: string[]) => Promise<any>;
    /** Callback to reload skills data after group update */
    onRefresh?: () => void;
    /** Save Basic Info / Metadata edits. Called from the in-panel
     *  Save buttons on the two editable sections. Receives a
     *  partial config — only the fields the section owns. Parent
     *  pipes through to PATCH /v1/admin/agent-skills/{id}. Optional:
     *  the Save buttons are hidden when this is undefined. */
    onSave?: (
        data: Partial<AgentSkillConfig> & { metadata?: Record<string, unknown>; group_ids?: string[] },
    ) => Promise<string | void>;
}

// ── Skill Structure tree ────────────────────────────────────────────────────
// Bundled files store their `filename` as the path relative to the skill ROOT
// (e.g. `scripts/office/foo.py`, `templates/letter.docx`, `LICENSE.txt`) — the
// same convention the ZIP importer + source-sync use so export round-trips
// faithfully. The Overview tree therefore reconstructs the ACTUAL directory
// layout from those paths rather than grouping by the coarse
// script/reference/asset bucket; grouping-by-bucket + printing the full path
// double-prefixed nested files (`scripts/scripts/foo.py`) and buried
// non-canonical dirs like `templates/` under `assets/`.

type SkillTreeKind = 'script' | 'reference' | 'asset';
type SkillTreeTab = 'scripts' | 'resources' | 'assets';

interface SkillTreeFileEntry {
    id: string;
    path: string;          // root-relative path (the stored filename)
    kind: SkillTreeKind;
    meta?: string;         // language / resource_type / asset_type
    tab: SkillTreeTab;     // which tab to open on click
}

interface SkillTreeDir {
    name: string;
    dirs: SkillTreeDir[];
    files: { name: string; entry: SkillTreeFileEntry }[];
}

function buildSkillFileTree(entries: SkillTreeFileEntry[]): SkillTreeDir {
    const root: SkillTreeDir = { name: '', dirs: [], files: [] };
    for (const entry of entries) {
        const parts = entry.path.split('/').filter(Boolean);
        if (parts.length === 0) continue;
        let node = root;
        for (let i = 0; i < parts.length - 1; i++) {
            const seg = parts[i];
            let child = node.dirs.find((d) => d.name === seg);
            if (!child) {
                child = { name: seg, dirs: [], files: [] };
                node.dirs.push(child);
            }
            node = child;
        }
        node.files.push({ name: parts[parts.length - 1], entry });
    }
    const sortRec = (d: SkillTreeDir) => {
        d.dirs.sort((a, b) => a.name.localeCompare(b.name));
        d.files.sort((a, b) => a.name.localeCompare(b.name));
        d.dirs.forEach(sortRec);
    };
    sortRec(root);
    return root;
}

/** Recursively render a skill's reconstructed directory tree. Directories
 *  first, then files; the last child of each level gets the `└──` connector.
 *  Indentation is depth-driven so arbitrarily-nested paths render correctly. */
const SkillTreeRows: React.FC<{
    dir: SkillTreeDir;
    depth: number;
    onOpenTab: (tab: SkillTreeTab) => void;
}> = ({ dir, depth, onOpenTab }) => {
    const total = dir.dirs.length + dir.files.length;
    let i = 0;
    const connector = () => (i === total - 1 ? '└──' : '├──');
    const out: React.ReactNode[] = [];

    for (const sub of dir.dirs) {
        const conn = connector();
        i++;
        out.push(
            <div key={`d-${depth}-${sub.name}`} className="mt-0.5 flex items-center space-x-2" style={{ paddingLeft: depth * 20 }}>
                <span className="text-disabled">{conn}</span>
                <FolderOpen className="w-3.5 h-3.5 text-amber-500" />
                <span className="text-xs text-secondary font-semibold">{sub.name}/</span>
            </div>,
        );
        out.push(<SkillTreeRows key={`dt-${depth}-${sub.name}`} dir={sub} depth={depth + 1} onOpenTab={onOpenTab} />);
    }
    for (const { name, entry } of dir.files) {
        const conn = connector();
        i++;
        const Icon = entry.kind === 'script' ? Terminal : entry.kind === 'reference' ? BookOpen : Image;
        const iconColor = entry.kind === 'script' ? 'text-amber-600' : entry.kind === 'reference' ? 'text-green-600' : 'text-purple-600';
        out.push(
            <div key={`f-${entry.id}`} className="mt-0.5 flex items-center space-x-2" style={{ paddingLeft: depth * 20 }}>
                <span className="text-disabled">{conn}</span>
                <Icon className={`w-3 h-3 ${iconColor}`} />
                <button
                    onClick={() => onOpenTab(entry.tab)}
                    className="text-xs text-secondary hover:text-primary-600 hover:underline"
                >
                    {name}
                </button>
                {entry.meta && <span className="text-xs text-muted capitalize">({entry.meta})</span>}
            </div>,
        );
    }
    return <>{out}</>;
};

const AgentSkillDetailPanel: React.FC<AgentSkillDetailPanelProps> = ({
                                                                         skill,
                                                                         initialTab,
                                                                         onClose,
                                                                         onCopy,
                                                                         onDelete,
                                                                         onPublish,
                                                                         onValidate,
                                                                         onDeprecate,
                                                                         onArchive,
                                                                         loadExecutions,
                                                                         loadResources,
                                                                         loadScripts,
                                                                         loadAssets,
                                                                         getResource,
                                                                         createResource,
                                                                         updateResource,
                                                                         deleteResource,
                                                                         getScript,
                                                                         createScript,
                                                                         updateScript,
                                                                         deleteScript,
                                                                         getAsset,
                                                                         createAsset,
                                                                         updateAsset,
                                                                         deleteAsset,
                                                                         fetchGroupTree,
                                                                         onUpdateGroups,
                                                                         onRefresh,
                                                                         onSave,
                                                                     }) => {
    const confirm = useConfirm();
    const { gatewayClient } = useGateway();
    const [activeTab, setActiveTab] = useState<DetailTab>(initialTab || 'overview');
    const [executions, setExecutions] = useState<ExecutionSummary[]>([]);
    const [executionTotal, setExecutionTotal] = useState(0);
    const [loadingExecutions, setLoadingExecutions] = useState(false);
    const [deprecateReason, setDeprecateReason] = useState('');
    const [showDeprecateForm, setShowDeprecateForm] = useState(false);

    // Resources / Scripts / Assets state
    const [resources, setResources] = useState<AgentSkillResource[]>([]);
    const [loadingResources, setLoadingResources] = useState(false);
    const [scripts, setScripts] = useState<AgentSkillScript[]>([]);
    const [loadingScripts, setLoadingScripts] = useState(false);
    const [assets, setAssets] = useState<AgentSkillAsset[]>([]);
    const [loadingAssets, setLoadingAssets] = useState(false);

    // Group editing state
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>([]);
    const [loadingGroupTree, setLoadingGroupTree] = useState(false);
    const [readFailure, reads] = useReadFailures<'executions' | 'resources' | 'scripts' | 'assets' | 'groups'>();
    const [asked, setAsked] = useState(0);
    const askAgain = () => setAsked((n) => n + 1);
    const [savingGroups, setSavingGroups] = useState(false);
    const [groupsDirty, setGroupsDirty] = useState(false);

    // Expandable content sections + lazy-loaded content
    const [expandedResourceId, setExpandedResourceId] = useState<string | null>(null);
    const [expandedScriptId, setExpandedScriptId] = useState<string | null>(null);
    const [expandedAssetId, setExpandedAssetId] = useState<string | null>(null);
    const [loadedContent, setLoadedContent] = useState<Record<string, any>>({});
    const [loadingContentId, setLoadingContentId] = useState<string | null>(null);

    // Edit mode
    // Inline forms
    const [showResourceForm, setShowResourceForm] = useState(false);
    const [editingResource, setEditingResource] = useState<AgentSkillResource | null>(null);
    const [resourceForm, setResourceForm] = useState({ filename: '', resource_type: 'reference', content: '', mime_type: '' });

    const [showScriptForm, setShowScriptForm] = useState(false);
    const [editingScript, setEditingScript] = useState<AgentSkillScript | null>(null);
    const [scriptForm, setScriptForm] = useState({ filename: '', description: '', script_content: '', language: 'python', execution_mode: 'sandbox' });

    const [showAssetForm, setShowAssetForm] = useState(false);
    const [editingAsset, setEditingAsset] = useState<AgentSkillAsset | null>(null);
    const [assetForm, setAssetForm] = useState({ filename: '', description: '', asset_type: 'data', content: '', mime_type: '', encoding: 'utf-8' });

    // -----------------------------------------------------------------
    // Basic Info / Metadata editable state. Initialised from `skill` and
    // re-synced via the useEffect below when the panel is reused for
    // a different skill row. Edits are gated on `isDraft` — published
    // skills render the same fields as read-only.
    // -----------------------------------------------------------------
    const [name, setName] = useState(skill.name || '');
    const [description, setDescription] = useState(skill.description || '');
    const [version, setVersion] = useState(skill.version || '1.0.0');
    const [disclosureMode, setDisclosureMode] = useState<SkillDisclosureMode>(
        skill.disclosure_mode || 'progressive',
    );
    const [license, setLicense] = useState(skill.license || '');
    const [enabled, setEnabled] = useState(skill.enabled ?? true);
    const [compatibility, setCompatibility] = useState(
        (skill.compatibility || []).join(', '),
    );
    const [allowedTools, setAllowedTools] = useState(
        (skill.allowed_tools || []).join(', '),
    );
    const [categoryEdit, setCategoryEdit] = useState<string>(
        (skill.metadata?.category as string) || '',
    );
    const [iconName, setIconName] = useState<string>(
        (skill.metadata?.icon_name as string) || 'Sparkles',
    );
    const [costEstimate, setCostEstimate] = useState<string>(
        skill.metadata?.cost_estimate_usd != null
            ? String(skill.metadata.cost_estimate_usd)
            : '',
    );
    const [approvalRequired, setApprovalRequired] = useState<boolean>(
        Boolean(skill.metadata?.approval_required),
    );
    const [rateLimitTier, setRateLimitTier] = useState<string>(
        (skill.metadata?.rate_limit_tier as string) || 'standard',
    );
    const [tagsInput, setTagsInput] = useState(
        ((skill.metadata?.tags as string[]) || []).join(', '),
    );
    const [requiredMcpServers, setRequiredMcpServers] = useState(
        ((skill.metadata?.required_mcp_servers as string[]) || []).join(', '),
    );
    const [requiredLlmModels, setRequiredLlmModels] = useState(
        ((skill.metadata?.required_llm_models as string[]) || []).join(', '),
    );
    // SKILL.md content — editable in create mode and for drafts. Existing
    // skills hydrate from skill.skill_md_content; brand-new skills get the
    // template default.
    const DEFAULT_SKILL_MD = `---\nname: my-skill\ndescription: What this skill does\n---\n\n# My Skill\n\nDescribe how the agent should use this skill.\n`;
    const [skillMdContent, setSkillMdContent] = useState<string>(
        skill.skill_md_content || (skill.id ? '' : DEFAULT_SKILL_MD),
    );
    const [editErrors, setEditErrors] = useState<ValidationErrors>({});
    const [savingEdits, setSavingEdits] = useState(false);
    // Tenant-wide default Limits / Policies (min-wins with each Resource Group).
    const [tenantLimits, setTenantLimits] = useState<Record<string, any>>(
        (skill.tenant_limits as Record<string, any>) || {},
    );
    // A skill's own governance is not acted on in this release: it is not
    // offered, and one stored before is shown read-only on the Limits tab.
    const storedTenantGovernance = Object.entries((skill.tenant_governance as Record<string, unknown> | null) ?? {}).filter(
        ([, value]) => value !== null && value !== undefined,
    );

    // Re-sync editable state when the panel switches to a different
    // skill row (parent reuses the panel via the `skill` prop swap).
    // Intentionally keyed on `skill.id` only — running this on every
    // skill field change would clobber in-flight edits.
    useEffect(() => {
        setName(skill.name || '');
        setDescription(skill.description || '');
        setVersion(skill.version || '1.0.0');
        setDisclosureMode(skill.disclosure_mode || 'progressive');
        setLicense(skill.license || '');
        setEnabled(skill.enabled ?? true);
        setCompatibility((skill.compatibility || []).join(', '));
        setAllowedTools((skill.allowed_tools || []).join(', '));
        setCategoryEdit((skill.metadata?.category as string) || '');
        setIconName((skill.metadata?.icon_name as string) || 'Sparkles');
        setCostEstimate(
            skill.metadata?.cost_estimate_usd != null
                ? String(skill.metadata.cost_estimate_usd)
                : '',
        );
        setApprovalRequired(Boolean(skill.metadata?.approval_required));
        setRateLimitTier((skill.metadata?.rate_limit_tier as string) || 'standard');
        setTagsInput(((skill.metadata?.tags as string[]) || []).join(', '));
        setRequiredMcpServers(
            ((skill.metadata?.required_mcp_servers as string[]) || []).join(', '),
        );
        setRequiredLlmModels(
            ((skill.metadata?.required_llm_models as string[]) || []).join(', '),
        );
        setSkillMdContent(
            skill.skill_md_content || (skill.id ? '' : DEFAULT_SKILL_MD),
        );
        setEditErrors({});
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [skill.id]);

    const category = getSkillCategory(skill);
    const tags = getSkillTags(skill);
    // Create mode is detected by an empty/missing skill id. The parent
    // passes a stub skill ({ id: '', status: 'draft', ... }) when the
    // user clicks "+ Create Skill" — that lets the same panel drive
    // create and edit without a separate modal.
    const isCreating = !skill.id;
    const isDraft = skill.status === 'draft' || skill.status === 'validated';
    const isPublished = skill.status === 'published' && !isCreating;
    const isDeprecated = skill.status === 'deprecated' && !isCreating;
    const isArchived = skill.status === 'archived' && !isCreating;
    const isComposite = skill.metadata?.skill_type === 'composite';
    const canEdit = (isDraft || isCreating) && !isPublished && !isArchived;

    // Load executions when tab changes (skip in create mode — no id)
    useEffect(() => {
        if (activeTab === 'executions' && loadExecutions && skill.id) {
            const load = async () => {
                setLoadingExecutions(true);
                try {
                    const result = await loadExecutions(skill.id, 0, 20);
                    setExecutions(result.executions);
                    setExecutionTotal(result.total);
                    reads.loaded('executions');
                } catch (err) {
                    reads.failed('executions', 'Failed to load the executions of this skill', err);
                } finally {
                    setLoadingExecutions(false);
                }
            };
            runBackground(load(), 'load');
        }
    }, [activeTab, skill.id, asked]);

    // Load resources when tab opens (also for overview structure tree).
    // Skipped in create mode — items are buffered locally instead.
    useEffect(() => {
        if ((activeTab === 'resources' || activeTab === 'overview') && loadResources && resources.length === 0 && skill.id) {
            const load = async () => {
                setLoadingResources(true);
                try {
                    const result = await loadResources(skill.id);
                    setResources(result);
                    reads.loaded('resources');
                } catch (err) {
                    reads.failed('resources', 'Failed to load the resources of this skill', err);
                } finally {
                    setLoadingResources(false);
                }
            };
            runBackground(load(), 'load');
        }
    }, [activeTab, skill.id, asked]);

    // Load scripts when tab opens (also for overview structure tree).
    // Skipped in create mode — items are buffered locally instead.
    useEffect(() => {
        if ((activeTab === 'scripts' || activeTab === 'overview') && loadScripts && scripts.length === 0 && skill.id) {
            const load = async () => {
                setLoadingScripts(true);
                try {
                    const result = await loadScripts(skill.id);
                    setScripts(result);
                    reads.loaded('scripts');
                } catch (err) {
                    reads.failed('scripts', 'Failed to load the scripts of this skill', err);
                } finally {
                    setLoadingScripts(false);
                }
            };
            runBackground(load(), 'load');
        }
    }, [activeTab, skill.id, asked]);

    // Load assets when tab opens (also for overview structure tree).
    // Skipped in create mode — items are buffered locally instead.
    useEffect(() => {
        if ((activeTab === 'assets' || activeTab === 'overview') && loadAssets && assets.length === 0 && skill.id) {
            const load = async () => {
                setLoadingAssets(true);
                try {
                    const result = await loadAssets(skill.id);
                    setAssets(result);
                    reads.loaded('assets');
                } catch (err) {
                    reads.failed('assets', 'Failed to load the assets of this skill', err);
                } finally {
                    setLoadingAssets(false);
                }
            };
            runBackground(load(), 'load');
        }
    }, [activeTab, skill.id, asked]);

    // Load group tree when groups tab opens
    useEffect(() => {
        if (activeTab === 'groups' && fetchGroupTree && groupTree.length === 0) {
            const load = async () => {
                setLoadingGroupTree(true);
                try {
                    const tree = await fetchGroupTree();
                    setGroupTree(tree);
                    // Initialize selected group IDs from skill's current groups
                    setSelectedGroupIds((skill.groups || []).map(g => g.id));
                    setGroupsDirty(false);
                    reads.loaded('groups');
                } catch (err) {
                    reads.failed('groups', 'Failed to load the resource groups', err);
                } finally {
                    setLoadingGroupTree(false);
                }
            };
            runBackground(load(), 'load');
        }
    }, [activeTab, skill.id, asked]);

    const handleSaveGroups = async () => {
        // In create mode there's no skill row yet — group_ids is bundled
        // into the create payload by handleCreate. Saving here would be
        // a no-op, so just clear the dirty flag with feedback.
        if (isCreating) {
            setGroupsDirty(false);
            toast.success('Access updated (will apply on Create Skill)');
            return;
        }
        if (!onUpdateGroups) return;
        setSavingGroups(true);
        try {
            await onUpdateGroups(skill.id, selectedGroupIds);
            toast.success('Group access updated');
            setGroupsDirty(false);
            onRefresh?.();
        } catch (err) {
            toast.error(errorMessage(err, 'Failed to update group access'));
        } finally {
            setSavingGroups(false);
        }
    };

    const handleGroupSelectionChange = (ids: string[]) => {
        setSelectedGroupIds(ids);
        const currentIds = (skill.groups || []).map(g => g.id).sort().join(',');
        const newIds = [...ids].sort().join(',');
        setGroupsDirty(currentIds !== newIds);
    };

    // ─── Lazy-load content on expand ──────────────────────────────────
    const loadItemContent = async (type: 'resource' | 'script' | 'asset', itemId: string) => {
        if (loadedContent[itemId]) return; // already cached
        const getter = type === 'resource' ? getResource : type === 'script' ? getScript : getAsset;
        if (!getter) return;
        setLoadingContentId(itemId);
        try {
            const full = await getter(skill.id, itemId);
            setLoadedContent(prev => ({ ...prev, [itemId]: full }));
        } catch (err) {
            console.error(`Failed to load ${type} content:`, errorKind(err));
            toast.error(failureSentence(`Failed to load ${type} content`, err));
        } finally {
            setLoadingContentId(null);
        }
    };

    const handleExpandResource = (id: string) => {
        const next = expandedResourceId === id ? null : id;
        setExpandedResourceId(next);
        if (next) runBackground(loadItemContent('resource', id), 'item content');
    };
    const handleExpandScript = (id: string) => {
        const next = expandedScriptId === id ? null : id;
        setExpandedScriptId(next);
        if (next) runBackground(loadItemContent('script', id), 'item content');
    };
    const handleExpandAsset = (id: string) => {
        const next = expandedAssetId === id ? null : id;
        setExpandedAssetId(next);
        if (next) runBackground(loadItemContent('asset', id), 'item content');
    };

    // ─── Resource CRUD ────────────────────────────────────────────────
    const resetResourceForm = () => { setResourceForm({ filename: '', resource_type: 'reference', content: '', mime_type: '' }); setEditingResource(null); setShowResourceForm(false); };
    const reloadResources = async () => { if (!loadResources) return; setLoadingResources(true); try { setResources(await loadResources(skill.id)); } finally { setLoadingResources(false); } };
    const handleResourceSave = async () => {
        if (!resourceForm.filename.trim() || !resourceForm.content.trim()) { toast.error('Filename and content are required'); return; }
        // Create mode: buffer in local pending state. The chain-POST
        // in handleCreate persists them once the skill row exists.
        if (isCreating) {
            if (editingResource) {
                setResources(prev => prev.map(r => r.id === editingResource.id
                    ? { ...r, content: resourceForm.content, resource_type: resourceForm.resource_type as AgentSkillResource['resource_type'], mime_type: resourceForm.mime_type || undefined }
                    : r));
                toast.success('Resource updated (pending)');
            } else {
                const tempId = `pending-r-${Date.now()}-${Math.random().toString(36).slice(2,8)}`;
                setResources(prev => [...prev, {
                    id: tempId,
                    skill_id: '',
                    filename: resourceForm.filename.trim(),
                    resource_type: resourceForm.resource_type as AgentSkillResource['resource_type'],
                    content_hash: '',
                    content: resourceForm.content,
                    mime_type: resourceForm.mime_type || undefined,
                    size_bytes: resourceForm.content.length,
                }]);
                toast.success('Resource added (will be created on Create Skill)');
            }
            resetResourceForm();
            return;
        }
        try {
            if (editingResource && updateResource) {
                await updateResource(skill.id, editingResource.id, { content: resourceForm.content, resource_type: resourceForm.resource_type, mime_type: resourceForm.mime_type || undefined });
                setLoadedContent(prev => { const n = { ...prev }; delete n[editingResource.id]; return n; });
                toast.success(`Resource updated`);
            } else if (createResource) {
                await createResource(skill.id, { filename: resourceForm.filename.trim(), resource_type: resourceForm.resource_type, content: resourceForm.content, mime_type: resourceForm.mime_type || undefined });
                toast.success(`Resource created`);
            }
            resetResourceForm(); runBackground(reloadResources(), 'reload resources');
        } catch (err) { toast.error(errorMessage(err, 'Failed to save resource')); }
    };
    const handleResourceEdit = async (res: AgentSkillResource) => {
        // Create mode: item already fully in local state — no fetch needed
        if (isCreating) {
            setResourceForm({ filename: res.filename, resource_type: res.resource_type || 'reference', content: res.content || '', mime_type: res.mime_type || '' });
            setEditingResource(res); setShowResourceForm(true);
            return;
        }
        if (!getResource) return;
        try {
            const full = await getResource(skill.id, res.id);
            setResourceForm({ filename: full.filename, resource_type: full.resource_type || 'reference', content: full.content || '', mime_type: full.mime_type || '' });
            setEditingResource(full); setShowResourceForm(true);
        } catch (err) { toast.error(failureSentence('Failed to load resource', err)); }
    };
    const handleResourceDelete = async (res: AgentSkillResource) => {
        if (isCreating) {
            if (!(await confirm({
                title: 'Remove resource?',
                message: `Resource "${res.filename}" will be dropped from this draft skill.`,
                confirmLabel: 'Remove',
            }))) return;
            setResources(prev => prev.filter(r => r.id !== res.id));
            toast.success('Resource removed');
            return;
        }
        if (!deleteResource) return;
        if (!(await confirm({
            title: 'Delete resource?',
            message: `Resource file "${res.filename}" will be removed from this skill.`,
            confirmLabel: 'Delete resource',
        }))) return;
        try { await deleteResource(skill.id, res.id); toast.success(`Resource deleted`); runBackground(reloadResources(), 'reload resources'); } catch (err) { toast.error(errorMessage(err, 'Failed to delete')); }
    };

    // ─── Script CRUD ──────────────────────────────────────────────────
    const resetScriptForm = () => { setScriptForm({ filename: '', description: '', script_content: '', language: 'python', execution_mode: 'sandbox' }); setEditingScript(null); setShowScriptForm(false); };
    const reloadScripts = async () => { if (!loadScripts) return; setLoadingScripts(true); try { setScripts(await loadScripts(skill.id)); } finally { setLoadingScripts(false); } };
    const handleScriptSave = async () => {
        if (!scriptForm.filename.trim() || !scriptForm.script_content.trim()) { toast.error('Filename and script content are required'); return; }
        if (isCreating) {
            if (editingScript) {
                setScripts(prev => prev.map(s => s.id === editingScript.id
                    ? { ...s, script_content: scriptForm.script_content, description: scriptForm.description || undefined, language: scriptForm.language as AgentSkillScript['language'], execution_mode: scriptForm.execution_mode as AgentSkillScript['execution_mode'] }
                    : s));
                toast.success('Script updated (pending)');
            } else {
                const tempId = `pending-s-${Date.now()}-${Math.random().toString(36).slice(2,8)}`;
                setScripts(prev => [...prev, {
                    id: tempId,
                    skill_id: '',
                    filename: scriptForm.filename.trim(),
                    description: scriptForm.description || undefined,
                    language: scriptForm.language as AgentSkillScript['language'],
                    execution_mode: scriptForm.execution_mode as AgentSkillScript['execution_mode'],
                    script_hash: '',
                    script_content: scriptForm.script_content,
                }]);
                toast.success('Script added (will be created on Create Skill)');
            }
            resetScriptForm();
            return;
        }
        try {
            if (editingScript && updateScript) {
                await updateScript(skill.id, editingScript.id, { script_content: scriptForm.script_content, description: scriptForm.description || undefined, language: scriptForm.language, execution_mode: scriptForm.execution_mode });
                setLoadedContent(prev => { const n = { ...prev }; delete n[editingScript.id]; return n; });
                toast.success(`Script updated`);
            } else if (createScript) {
                await createScript(skill.id, { filename: scriptForm.filename.trim(), description: scriptForm.description || undefined, script_content: scriptForm.script_content, language: scriptForm.language, execution_mode: scriptForm.execution_mode });
                toast.success(`Script created`);
            }
            resetScriptForm(); runBackground(reloadScripts(), 'reload scripts');
        } catch (err) { toast.error(errorMessage(err, 'Failed to save script')); }
    };
    const handleScriptEdit = async (scr: AgentSkillScript) => {
        if (isCreating) {
            setScriptForm({ filename: scr.filename, description: scr.description || '', script_content: scr.script_content || '', language: scr.language || 'python', execution_mode: scr.execution_mode || 'sandbox' });
            setEditingScript(scr); setShowScriptForm(true);
            return;
        }
        if (!getScript) return;
        try {
            const full = await getScript(skill.id, scr.id);
            setScriptForm({ filename: full.filename, description: full.description || '', script_content: full.script_content || '', language: full.language || 'python', execution_mode: full.execution_mode || 'sandbox' });
            setEditingScript(full); setShowScriptForm(true);
        } catch (err) { toast.error(failureSentence('Failed to load script', err)); }
    };
    const handleScriptDelete = async (scr: AgentSkillScript) => {
        if (isCreating) {
            if (!(await confirm({
                title: 'Remove script?',
                message: `Script "${scr.filename}" will be dropped from this draft skill.`,
                confirmLabel: 'Remove',
            }))) return;
            setScripts(prev => prev.filter(s => s.id !== scr.id));
            toast.success('Script removed');
            return;
        }
        if (!deleteScript) return;
        if (!(await confirm({
            title: 'Delete script?',
            message: `Script "${scr.filename}" will be removed from this skill.`,
            confirmLabel: 'Delete script',
        }))) return;
        try { await deleteScript(skill.id, scr.id); toast.success(`Script deleted`); runBackground(reloadScripts(), 'reload scripts'); } catch (err) { toast.error(errorMessage(err, 'Failed to delete')); }
    };

    // ─── Asset CRUD ───────────────────────────────────────────────────
    const resetAssetForm = () => { setAssetForm({ filename: '', description: '', asset_type: 'data', content: '', mime_type: '', encoding: 'utf-8' }); setEditingAsset(null); setShowAssetForm(false); };
    const reloadAssets = async () => { if (!loadAssets) return; setLoadingAssets(true); try { setAssets(await loadAssets(skill.id)); } finally { setLoadingAssets(false); } };
    const handleAssetSave = async () => {
        if (!assetForm.filename.trim() || !assetForm.content.trim()) { toast.error('Filename and content are required'); return; }
        if (isCreating) {
            if (editingAsset) {
                setAssets(prev => prev.map(a => a.id === editingAsset.id
                    ? { ...a, content: assetForm.content, description: assetForm.description || undefined, asset_type: assetForm.asset_type as AgentSkillAsset['asset_type'], mime_type: assetForm.mime_type || undefined, encoding: assetForm.encoding }
                    : a));
                toast.success('Asset updated (pending)');
            } else {
                const tempId = `pending-a-${Date.now()}-${Math.random().toString(36).slice(2,8)}`;
                setAssets(prev => [...prev, {
                    id: tempId,
                    skill_id: '',
                    filename: assetForm.filename.trim(),
                    description: assetForm.description || undefined,
                    asset_type: assetForm.asset_type as AgentSkillAsset['asset_type'],
                    content_hash: '',
                    encoding: assetForm.encoding,
                    content: assetForm.content,
                    mime_type: assetForm.mime_type || undefined,
                    size_bytes: assetForm.content.length,
                }]);
                toast.success('Asset added (will be created on Create Skill)');
            }
            resetAssetForm();
            return;
        }
        try {
            if (editingAsset && updateAsset) {
                await updateAsset(skill.id, editingAsset.id, { content: assetForm.content, description: assetForm.description || undefined, asset_type: assetForm.asset_type, mime_type: assetForm.mime_type || undefined, encoding: assetForm.encoding });
                setLoadedContent(prev => { const n = { ...prev }; delete n[editingAsset.id]; return n; });
                toast.success(`Asset updated`);
            } else if (createAsset) {
                await createAsset(skill.id, { filename: assetForm.filename.trim(), description: assetForm.description || undefined, asset_type: assetForm.asset_type, content: assetForm.content, mime_type: assetForm.mime_type || undefined, encoding: assetForm.encoding });
                toast.success(`Asset created`);
            }
            resetAssetForm(); runBackground(reloadAssets(), 'reload assets');
        } catch (err) { toast.error(errorMessage(err, 'Failed to save asset')); }
    };
    const handleAssetEdit = async (asset: AgentSkillAsset) => {
        if (isCreating) {
            setAssetForm({ filename: asset.filename, description: asset.description || '', asset_type: asset.asset_type || 'data', content: asset.content || '', mime_type: asset.mime_type || '', encoding: asset.encoding || 'utf-8' });
            setEditingAsset(asset); setShowAssetForm(true);
            return;
        }
        if (!getAsset) return;
        try {
            const full = await getAsset(skill.id, asset.id);
            setAssetForm({ filename: full.filename, description: full.description || '', asset_type: full.asset_type || 'data', content: full.content || '', mime_type: full.mime_type || '', encoding: full.encoding || 'utf-8' });
            setEditingAsset(full); setShowAssetForm(true);
        } catch (err) { toast.error(failureSentence('Failed to load asset', err)); }
    };
    const handleAssetDelete = async (asset: AgentSkillAsset) => {
        if (isCreating) {
            if (!(await confirm({
                title: 'Remove asset?',
                message: `Asset "${asset.filename}" will be dropped from this draft skill.`,
                confirmLabel: 'Remove',
            }))) return;
            setAssets(prev => prev.filter(a => a.id !== asset.id));
            toast.success('Asset removed');
            return;
        }
        if (!deleteAsset) return;
        if (!(await confirm({
            title: 'Delete asset?',
            message: `Asset "${asset.filename}" will be removed from this skill.`,
            confirmLabel: 'Delete asset',
        }))) return;
        try { await deleteAsset(skill.id, asset.id); toast.success(`Asset deleted`); runBackground(reloadAssets(), 'reload assets'); } catch (err) { toast.error(errorMessage(err, 'Failed to delete')); }
    };

    // Same tab layout for create / edit / view, except Executions is
    // hidden in create mode (no executions yet). Resources / Scripts /
    // Assets in create mode are buffered to local pending arrays — they
    // get POSTed to the new skill row right after the create call returns.
    const tabs: { id: DetailTab; label: string; icon: React.FC<{ className?: string }> }[] = [
        { id: 'overview', label: 'Overview', icon: Info },
        { id: 'basic-info', label: 'Basic Info', icon: Settings },
        { id: 'audit', label: 'Audit Info', icon: FileClock },
        { id: 'metadata', label: 'Metadata & Tags', icon: Tag },
        { id: 'groups', label: 'Access Control', icon: Shield },
        { id: 'content', label: 'SKILL.md', icon: FileCode },
        { id: 'resources', label: 'Resources', icon: FolderOpen },
        { id: 'scripts', label: 'Scripts', icon: Code2 },
        { id: 'assets', label: 'Assets', icon: Package },
        ...(onSave ? [
            { id: 'limits' as DetailTab, label: 'Limits', icon: Gauge },
        ] : []),
        ...(isCreating ? [] : [{ id: 'executions' as DetailTab, label: 'Executions', icon: Activity }]),
    ];

    // -----------------------------------------------------------------
    // Basic Info / Metadata save handlers.
    // -----------------------------------------------------------------
    const parseCsv = (s: string): string[] =>
        s.split(',').map((t) => t.trim()).filter(Boolean);

    const validateBasicInfo = (): boolean => {
        const errs: ValidationErrors = {};
        if (!name.trim()) {
            errs.name = 'Skill name is required';
        } else if (!isValidSkillName(name)) {
            errs.name =
                'Must be 2-64 chars, lowercase letters/digits/hyphens, start with letter, end with letter/digit';
        }
        if (!description.trim()) {
            errs.description = 'Description is required';
        } else if (description.length > 1024) {
            errs.description = 'Description must be 1024 characters or less';
        }
        if (!version.trim()) {
            errs.version = 'Version is required';
        } else if (!/^\d+\.\d+\.\d+$/.test(version)) {
            errs.version = 'Must be semantic version (e.g. 1.0.0)';
        }
        setEditErrors(errs);
        return Object.keys(errs).length === 0;
    };

    const validateMetadata = (): boolean => {
        const errs: ValidationErrors = {};
        if (costEstimate && (isNaN(parseFloat(costEstimate)) || parseFloat(costEstimate) < 0)) {
            errs.cost_estimate = 'Must be a non-negative number';
        }
        setEditErrors(errs);
        return Object.keys(errs).length === 0;
    };

    const handleSaveBasicInfo = async () => {
        if (!onSave) return;
        if (!validateBasicInfo()) {
            toast.error('Please fix validation errors');
            return;
        }
        setSavingEdits(true);
        try {
            // Preserve existing metadata — Basic Info doesn't own metadata
            // keys, but the PATCH endpoint replaces metadata wholesale, so
            // we resend whatever's on the skill plus any in-flight Metadata
            // edits already in the form state. The Metadata save handler
            // does the same in reverse.
            await onSave({
                name: name.trim(),
                description: description.trim(),
                version: version.trim(),
                license: license || undefined,
                enabled,
                disclosure_mode: disclosureMode,
                compatibility: compatibility ? parseCsv(compatibility) : undefined,
                allowed_tools: allowedTools ? parseCsv(allowedTools) : undefined,
            });
            toast.success('Basic info saved');
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error(`Save failed: ${msg}`);
        } finally {
            setSavingEdits(false);
        }
    };

    const handleSaveLimits = async () => {
        if (!onSave) return;
        setSavingEdits(true);
        try {
            await onSave({ tenant_limits: tenantLimits });
            toast.success('Limits saved');
        } catch (e) {
            toast.error(`Save failed: ${e instanceof Error ? e.message : String(e)}`);
        } finally {
            setSavingEdits(false);
        }
    };

    const handleSaveMetadata = async () => {
        if (!onSave) return;
        if (!validateMetadata()) {
            toast.error('Please fix validation errors');
            return;
        }
        setSavingEdits(true);
        try {
            await onSave({
                metadata: {
                    ...(skill.metadata || {}),
                    category: categoryEdit || undefined,
                    icon_name: iconName || undefined,
                    cost_estimate_usd: costEstimate ? parseFloat(costEstimate) : undefined,
                    approval_required: approvalRequired || undefined,
                    // rate_limit_tier is typed as a string-union on the
                    // server-side Pydantic, but the <select> emits a
                    // bare string. The union narrowing happens at the
                    // boundary; the cast here narrows it for the payload.
                    rate_limit_tier: (rateLimitTier || undefined) as
                        | 'standard'
                        | 'premium'
                        | 'enterprise'
                        | undefined,
                    tags: tagsInput ? parseCsv(tagsInput) : undefined,
                    required_mcp_servers: requiredMcpServers ? parseCsv(requiredMcpServers) : undefined,
                    required_llm_models: requiredLlmModels ? parseCsv(requiredLlmModels) : undefined,
                },
            });
            toast.success('Metadata saved');
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error(`Save failed: ${msg}`);
        } finally {
            setSavingEdits(false);
        }
    };

    // Bundles Basic Info + Metadata + SKILL.md + Access Control into a
    // single create call, then chain-POSTs any pending Resources, Scripts,
    // and Assets the user added in the panel before clicking Create Skill.
    // Driven by the "Create Skill" header button when isCreating. Parent's
    // onSave is expected to return the new skill id on create (or undefined
    // on update); the panel uses that id to anchor the sub-resource POSTs.
    const handleCreate = async () => {
        if (!onSave) return;
        if (!validateBasicInfo()) {
            toast.error('Please fix validation errors');
            setActiveTab('basic-info');
            return;
        }
        if (!validateMetadata()) {
            toast.error('Please fix validation errors');
            setActiveTab('metadata');
            return;
        }
        if (!skillMdContent.trim()) {
            toast.error('SKILL.md content is required');
            setActiveTab('content');
            return;
        }
        setSavingEdits(true);
        try {
            const result = await onSave({
                name: name.trim(),
                description: description.trim(),
                version: version.trim(),
                license: license || undefined,
                enabled,
                disclosure_mode: disclosureMode,
                compatibility: compatibility ? parseCsv(compatibility) : undefined,
                allowed_tools: allowedTools ? parseCsv(allowedTools) : undefined,
                skill_md_content: skillMdContent,
                group_ids: selectedGroupIds,
                metadata: {
                    category: categoryEdit || undefined,
                    icon_name: iconName || undefined,
                    cost_estimate_usd: costEstimate ? parseFloat(costEstimate) : undefined,
                    approval_required: approvalRequired || undefined,
                    rate_limit_tier: (rateLimitTier || undefined) as
                        | 'standard'
                        | 'premium'
                        | 'enterprise'
                        | undefined,
                    tags: tagsInput ? parseCsv(tagsInput) : undefined,
                    required_mcp_servers: requiredMcpServers ? parseCsv(requiredMcpServers) : undefined,
                    required_llm_models: requiredLlmModels ? parseCsv(requiredLlmModels) : undefined,
                },
            });
            // The parent's create handler returns the new skill id. If
            // the user buffered any Resources / Scripts / Assets, POST
            // them now. Failures here don't roll back the skill row —
            // the user can retry from the (now-saved) panel.
            const newSkillId = typeof result === 'string' ? result : undefined;
            if (newSkillId && (resources.length || scripts.length || assets.length)) {
                let failures = 0;
                for (const r of resources) {
                    if (!createResource) break;
                    try {
                        await createResource(newSkillId, {
                            filename: r.filename,
                            resource_type: r.resource_type,
                            content: r.content || '',
                            mime_type: r.mime_type,
                        });
                    } catch (e) {
                        failures++;
                        const msg = e instanceof Error ? e.message : String(e);
                        toast.error(`Resource "${r.filename}": ${msg}`);
                    }
                }
                for (const s of scripts) {
                    if (!createScript) break;
                    try {
                        await createScript(newSkillId, {
                            filename: s.filename,
                            description: s.description,
                            script_content: s.script_content || '',
                            language: s.language,
                            execution_mode: s.execution_mode,
                        });
                    } catch (e) {
                        failures++;
                        const msg = e instanceof Error ? e.message : String(e);
                        toast.error(`Script "${s.filename}": ${msg}`);
                    }
                }
                for (const a of assets) {
                    if (!createAsset) break;
                    try {
                        await createAsset(newSkillId, {
                            filename: a.filename,
                            description: a.description,
                            asset_type: a.asset_type,
                            content: a.content || '',
                            mime_type: a.mime_type,
                            encoding: a.encoding,
                        });
                    } catch (e) {
                        failures++;
                        const msg = e instanceof Error ? e.message : String(e);
                        toast.error(`Asset "${a.filename}": ${msg}`);
                    }
                }
                if (failures === 0) {
                    toast.success(`Skill created with ${resources.length} resource(s), ${scripts.length} script(s), ${assets.length} asset(s)`);
                }
            }
            onRefresh?.();
            onClose();
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error(`Create failed: ${msg}`);
        } finally {
            setSavingEdits(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
            {/* Backdrop */}
            <div className="absolute inset-0 bg-black/40" onClick={onClose} />

            {/* Modal */}
            <div className="relative bg-surface rounded-xl shadow-2xl flex flex-col w-[94vw] h-[92vh] max-w-[1600px] overflow-hidden">

                {/* ── Header ── */}
                <div className="px-6 py-3.5 border-b border-default flex items-center justify-between flex-shrink-0">
                    <div className="flex items-center space-x-3 min-w-0 flex-1 min-h-0">
                        <div className={`p-1.5 rounded-lg flex-shrink-0 ${isCreating ? 'bg-primary-50' : (SKILL_STATUS_CONFIG[skill.status]?.bgColor || 'bg-surface-raised')}`}>
                            <Sparkles className={`w-5 h-5 ${isCreating ? 'text-primary-600' : (SKILL_STATUS_CONFIG[skill.status]?.color || 'text-secondary')}`} />
                        </div>
                        <div className="min-w-0">
                            <div className="flex items-center space-x-2">
                                <h2 className="text-base font-semibold text-default truncate">
                                    {isCreating ? 'Create Agent Skill' : skill.name}
                                </h2>
                                {!isCreating && <AgentSkillStatusBadge status={skill.status} />}
                                {!isCreating && (isComposite ? (
                                    <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-xs font-medium bg-primary-100 text-primary-700 rounded-full">
                                        <Workflow className="w-3 h-3" /><span>Composite</span>
                                    </span>
                                ) : (
                                    <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-xs font-medium bg-surface-raised text-secondary rounded-full">
                                        <Sparkles className="w-3 h-3" /><span>Standalone</span>
                                    </span>
                                ))}
                                {!isCreating && (
                                    <span className="px-2 py-0.5 text-xs font-mono bg-surface-raised text-secondary rounded">v{skill.version}</span>
                                )}
                                {!isCreating && !skill.enabled && (
                                    <span className="px-2 py-0.5 text-xs font-medium text-muted bg-surface-raised rounded">Disabled</span>
                                )}
                            </div>
                            {isCreating ? (
                                <p className="text-xs text-muted mt-0.5">Fill in Basic Info, Metadata, and SKILL.md, then click Create Skill.</p>
                            ) : (
                                skill.description && (
                                    <p className="text-xs text-muted truncate mt-0.5 max-w-2xl">{skill.description}</p>
                                )
                            )}
                        </div>
                    </div>

                    {/* Action buttons */}
                    <div className="flex items-center space-x-1.5 flex-shrink-0 ml-4">
                        {isCreating ? (
                            <button
                                onClick={asyncHandler(handleCreate, 'click')}
                                disabled={savingEdits}
                                className="px-3 py-1.5 text-xs font-medium text-white bg-primary-600 hover:bg-primary-700 rounded-lg transition-colors flex items-center space-x-1 disabled:opacity-50"
                            >
                                {savingEdits ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                                <span>Create Skill</span>
                            </button>
                        ) : (
                            <>
                                {isDraft && (
                                    <>
                                        <button onClick={onValidate}
                                                className="px-2.5 py-1.5 text-xs font-medium text-primary-700 bg-primary-50 hover:bg-primary-100 rounded-lg transition-colors flex items-center space-x-1">
                                            <CheckCircle className="w-3.5 h-3.5" /><span>Validate</span>
                                        </button>
                                        <button onClick={onPublish}
                                                className="px-2.5 py-1.5 text-xs font-medium text-green-700 bg-green-50 hover:bg-green-100 rounded-lg transition-colors flex items-center space-x-1">
                                            <Globe className="w-3.5 h-3.5" /><span>Publish</span>
                                        </button>
                                    </>
                                )}
                                {isPublished && (
                                    <button onClick={() => setShowDeprecateForm(!showDeprecateForm)}
                                            className="px-2.5 py-1.5 text-xs font-medium text-amber-700 bg-amber-50 hover:bg-amber-100 rounded-lg transition-colors flex items-center space-x-1">
                                        <AlertTriangle className="w-3.5 h-3.5" /><span>Deprecate</span>
                                    </button>
                                )}
                                {!isArchived && (
                                    <button onClick={onArchive}
                                            className="px-2.5 py-1.5 text-xs font-medium text-secondary bg-surface-alt hover:bg-surface-raised rounded-lg transition-colors flex items-center space-x-1">
                                        <Archive className="w-3.5 h-3.5" /><span>Archive</span>
                                    </button>
                                )}
                                <button
                                    onClick={asyncHandler(async () => {
                                        try {
                                            const blob = await gatewayClient.exportAgentSkill(skill.id);
                                            // Bundle filename mirrors the server's
                                            // sanitize_filename() result so the
                                            // user sees the same name in their
                                            // ~/.claude/skills/ directory.
                                            const safeName = (skill.name || 'skill').replace(/[^a-zA-Z0-9._-]/g, '_');
                                            downloadBlobAsFile(blob, `${safeName}-${skill.version || '1.0.0'}.zip`);
                                            toast.success('Skill bundle downloaded — drop into ~/.claude/skills/');
                                        } catch (e) {
                                            const msg = e instanceof Error ? e.message : String(e);
                                            toast.error(`Export failed: ${msg}`);
                                        }
                                    }, 'click')}
                                    title="Download as SKILL.md ZIP (Claude Desktop, Cursor, Codex, VS Code, Kiro, Goose)"
                                    className="px-2.5 py-1.5 text-xs font-medium text-secondary bg-surface-alt hover:bg-surface-raised rounded-lg transition-colors flex items-center space-x-1">
                                    <Download className="w-3.5 h-3.5" /><span>Export</span>
                                </button>
                                <button onClick={onCopy}
                                        className="px-2.5 py-1.5 text-xs font-medium text-secondary bg-surface-alt hover:bg-surface-raised rounded-lg transition-colors flex items-center space-x-1">
                                    <Copy className="w-3.5 h-3.5" /><span>Duplicate</span>
                                </button>
                                <button onClick={onDelete}
                                        className="px-2.5 py-1.5 text-xs font-medium text-red-600 bg-red-50 hover:bg-red-100 rounded-lg transition-colors flex items-center space-x-1">
                                    <Trash2 className="w-3.5 h-3.5" /><span>Delete</span>
                                </button>
                            </>
                        )}

                        <div className="w-px h-6 bg-surface-inset mx-1" />
                        <button onClick={onClose}
                                className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors">
                            <X className="w-5 h-5" />
                        </button>
                    </div>
                </div>

                {/* Deprecate form (inline below header) */}
                {showDeprecateForm && (
                    <div className="px-6 py-3 bg-amber-50 border-b border-amber-200">
                        <label className="block text-xs font-medium text-amber-800 mb-1">Deprecation Reason</label>
                        <div className="flex items-center space-x-2">
                            <input
                                type="text"
                                value={deprecateReason}
                                onChange={(e) => setDeprecateReason(e.target.value)}
                                placeholder="e.g. Superseded by v2.0.0"
                                className="flex-1 px-3 py-1.5 text-sm border border-amber-300 rounded-md focus:ring-2 focus:ring-amber-500 min-h-0 min-w-0"
                            />
                            <button
                                onClick={() => {
                                    if (deprecateReason.trim()) {
                                        onDeprecate(deprecateReason.trim());
                                        setShowDeprecateForm(false);
                                        setDeprecateReason('');
                                    }
                                }}
                                disabled={!deprecateReason.trim()}
                                className="px-3 py-1.5 text-xs font-medium text-white bg-amber-600 hover:bg-amber-700 rounded-md disabled:opacity-50"
                            >
                                Confirm
                            </button>
                        </div>
                    </div>
                )}

                {/* ── Body: sidebar tabs + content ── */}
                <div className="flex flex-1 min-h-0 min-w-0">

                    {/* Left sidebar tabs */}
                    <nav className="w-44 flex-shrink-0 border-r border-default bg-surface-alt overflow-y-auto py-2">
                        {tabs.map(({ id, label, icon: Icon }) => (
                            <button
                                key={id}
                                onClick={() => setActiveTab(id)}
                                className={`w-full flex items-center space-x-2.5 px-4 py-2.5 text-left text-sm transition-colors ${
                                    activeTab === id
                                        ? 'bg-surface text-primary-700 font-medium border-r-2 border-primary-500 shadow-sm'
                                        : 'text-secondary hover:bg-surface-raised hover:text-default'
                                }`}
                            >
                                <Icon className="w-4 h-4 flex-shrink-0" />
                                <span className="truncate">{label}</span>
                            </button>
                        ))}
                    </nav>

                    {/* Right content area */}
                    <div className="scroll-region">

                        {/* ============= AUDIT INFO TAB ============= */}
                        {activeTab === 'audit' && (
                            <div className="p-6 max-w-2xl">
                                <AuditInfoTab
                                    // A skill's version is edited on Basic
                                    // Info, where it has always lived. Two
                                    // inputs bound to one field invite the
                                    // operator to change it in one place,
                                    // save from the other, and lose the edit.
                                    versionHidden
                                    versionDerivedFrom="Basic Info tab"
                                    meta={{
                                        created_by: skill.created_by,
                                        created_at: skill.created_at,
                                        updated_by: skill.updated_by,
                                        updated_at: skill.updated_at,
                                    }}
                                    isNew={isCreating}
                                />
                            </div>
                        )}

                        {activeTab === 'basic-info' && (
                            <div className="p-6 space-y-4 max-w-2xl">
                                {isPublished && (
                                    <div className="px-3 py-2 text-xs bg-amber-50 text-amber-800 border border-amber-200 rounded-md dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-900">
                                        Published skills are immutable. To change Basic Info, publish a new version instead.
                                    </div>
                                )}
                                <ValidatedInput
                                    label="Skill Name"
                                    value={name}
                                    onChange={setName}
                                    error={editErrors.name}
                                    required
                                    placeholder="query-sales-data"
                                    helpText="Lowercase letters, digits, hyphens. Must start with letter."
                                    disabled={isPublished}
                                />
                                <ValidatedTextarea
                                    label="Description"
                                    value={description}
                                    onChange={isPublished ? () => {} : setDescription}
                                    error={editErrors.description}
                                    required
                                    placeholder="What does this skill do? Describe the agent capability..."
                                    rows={3}
                                    maxLength={1024}
                                    showCharCount
                                />
                                <div className="grid grid-cols-2 gap-4">
                                    <ValidatedInput
                                        label="Version"
                                        value={version}
                                        onChange={setVersion}
                                        error={editErrors.version}
                                        required
                                        placeholder="1.0.0"
                                        helpText="Semantic versioning: major.minor.patch"
                                        disabled={isPublished}
                                    />
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">License</label>
                                        <select
                                            value={license}
                                            onChange={(e) => setLicense(e.target.value)}
                                            disabled={isPublished}
                                            className="w-full px-3 py-2 border border-strong rounded-md shadow-sm focus:ring-2 focus:ring-primary-500 focus:border-primary-500 bg-input"
                                        >
                                            <option value="">Select license...</option>
                                            {SKILL_LICENSES.map((l) => (
                                                <option key={l} value={l}>{l}</option>
                                            ))}
                                        </select>
                                    </div>
                                </div>
                                <ValidatedInput
                                    label="Compatible Agents"
                                    value={compatibility}
                                    onChange={setCompatibility}
                                    placeholder="claude-3, gpt-4, gemini-2 (comma-separated)"
                                    helpText="Agent identifiers this skill is designed for"
                                    disabled={isPublished}
                                />
                                <ValidatedInput
                                    label="Allowed Tools"
                                    value={allowedTools}
                                    onChange={setAllowedTools}
                                    placeholder="web-search, file-read, database-query (comma-separated)"
                                    helpText="Pre-approved tool names the skill may invoke"
                                    disabled={isPublished}
                                />
                                <div>
                                    <label htmlFor="basic-disclosure" className="block text-sm font-medium text-secondary mb-1">
                                        Disclosure mode
                                    </label>
                                    <select
                                        id="basic-disclosure"
                                        value={disclosureMode}
                                        onChange={(e) => setDisclosureMode(e.target.value as SkillDisclosureMode)}
                                        disabled={isPublished}
                                        className="w-full px-3 py-2 text-sm bg-surface border border-strong rounded-md focus:ring-primary-500 focus:border-primary-500"
                                    >
                                        {SKILL_DISCLOSURE_MODES.map((m) => (
                                            <option key={m.value} value={m.value}>{m.label}</option>
                                        ))}
                                    </select>
                                    <p className="mt-1 text-xs text-tertiary">
                                        {SKILL_DISCLOSURE_MODES.find((m) => m.value === disclosureMode)?.help}
                                    </p>
                                </div>
                                <div className="flex items-center space-x-3">
                                    <input
                                        type="checkbox"
                                        id="basic-enabled"
                                        checked={enabled}
                                        onChange={(e) => setEnabled(e.target.checked)}
                                        disabled={isPublished}
                                        className="h-4 w-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                                    />
                                    <label htmlFor="basic-enabled" className="text-sm text-secondary">
                                        Skill enabled (available for execution)
                                    </label>
                                </div>
                                {!isPublished && !isCreating && onSave && (
                                    <div className="pt-3 border-t border-default flex justify-end">
                                        <button
                                            onClick={asyncHandler(handleSaveBasicInfo, 'click')}
                                            disabled={savingEdits}
                                            className="px-4 py-2 text-sm font-medium bg-primary text-white rounded-md hover:bg-primary-600 disabled:opacity-50"
                                        >
                                            {savingEdits ? 'Saving…' : 'Save Basic Info'}
                                        </button>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= METADATA & TAGS TAB ============= */}
                        {activeTab === 'metadata' && (
                            <div className="p-6 space-y-4 max-w-2xl">
                                {isPublished && (
                                    <div className="px-3 py-2 text-xs bg-amber-50 text-amber-800 border border-amber-200 rounded-md dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-900">
                                        Published skills are immutable. Metadata changes require a new version.
                                    </div>
                                )}
                                <div className="grid grid-cols-2 gap-4">
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">Category</label>
                                        <select
                                            value={categoryEdit}
                                            onChange={(e) => setCategoryEdit(e.target.value)}
                                            disabled={isPublished}
                                            className="w-full px-3 py-2 border border-strong rounded-md shadow-sm focus:ring-2 focus:ring-primary-500 bg-input"
                                        >
                                            <option value="">Select category...</option>
                                            {SKILL_CATEGORIES.map((c) => (
                                                <option key={c} value={c}>{c}</option>
                                            ))}
                                        </select>
                                    </div>
                                    <ValidatedInput
                                        label="Icon Name (Lucide)"
                                        value={iconName}
                                        onChange={setIconName}
                                        placeholder="Sparkles"
                                        helpText="Lucide icon name for UI display"
                                        disabled={isPublished}
                                    />
                                </div>
                                <ValidatedInput
                                    label="Tags"
                                    value={tagsInput}
                                    onChange={setTagsInput}
                                    placeholder="analytics, reporting, sales (comma-separated)"
                                    helpText="Categorization tags for catalog filtering"
                                    disabled={isPublished}
                                />
                                <div className="grid grid-cols-2 gap-4">
                                    <ValidatedInput
                                        label="Cost Estimate (USD)"
                                        value={costEstimate}
                                        onChange={setCostEstimate}
                                        error={editErrors.cost_estimate}
                                        type="number"
                                        step="0.01"
                                        min="0"
                                        placeholder="0.05"
                                        helpText="Estimated cost per execution"
                                        disabled={isPublished}
                                    />
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">Rate Limit Tier</label>
                                        <select
                                            value={rateLimitTier}
                                            onChange={(e) => setRateLimitTier(e.target.value)}
                                            disabled={isPublished}
                                            className="w-full px-3 py-2 border border-strong rounded-md shadow-sm focus:ring-2 focus:ring-primary-500 bg-input"
                                        >
                                            <option value="standard">Standard</option>
                                            <option value="premium">Premium</option>
                                            <option value="enterprise">Enterprise</option>
                                        </select>
                                    </div>
                                </div>
                                <div className="flex items-center space-x-3">
                                    <input
                                        type="checkbox"
                                        id="approval-required"
                                        checked={approvalRequired}
                                        onChange={(e) => setApprovalRequired(e.target.checked)}
                                        disabled={isPublished}
                                        className="h-4 w-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                                    />
                                    <label htmlFor="approval-required" className="text-sm text-secondary">
                                        Require human approval before execution
                                    </label>
                                </div>
                                <div className="border-t border-default pt-4 mt-4">
                                    <h3 className="text-sm font-medium text-default mb-3">Resource Dependencies</h3>
                                    <ValidatedInput
                                        label="Required MCP Servers"
                                        value={requiredMcpServers}
                                        onChange={setRequiredMcpServers}
                                        placeholder="sc-01JABCD, sc-02XYZAB (comma-separated server config IDs)"
                                        helpText="MCP ServerConfig IDs this skill depends on"
                                        disabled={isPublished}
                                    />
                                    <div className="mt-3">
                                        <ValidatedInput
                                            label="Required LLM Models"
                                            value={requiredLlmModels}
                                            onChange={setRequiredLlmModels}
                                            placeholder="llm-01JABCD, rg-01XYZAB (comma-separated model/routing group IDs)"
                                            helpText="LLModel or RoutingGroup IDs this skill depends on"
                                            disabled={isPublished}
                                        />
                                    </div>
                                </div>
                                {!isPublished && !isCreating && onSave && (
                                    <div className="pt-3 border-t border-default flex justify-end">
                                        <button
                                            onClick={asyncHandler(handleSaveMetadata, 'click')}
                                            disabled={savingEdits}
                                            className="px-4 py-2 text-sm font-medium bg-primary text-white rounded-md hover:bg-primary-600 disabled:opacity-50"
                                        >
                                            {savingEdits ? 'Saving…' : 'Save Metadata'}
                                        </button>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= OVERVIEW TAB ============= */}
                        {activeTab === 'overview' && (
                            <div className="p-6 space-y-6 max-w-4xl">
                                {/* Info Grid */}
                                <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
                                    <InfoField label="ID" value={skill.id} mono />
                                    <InfoField label="Tenant" value={skill.tenant_id} mono />
                                    <InfoField label="Category" value={category} />
                                    <InfoField label="License" value={skill.license || 'None'} />
                                    <InfoField label="SKILL.md Hash" value={skill.skill_md_hash?.substring(0, 16) + '...'} mono />
                                    <InfoField label="Executions" value={skill.execution_count.toLocaleString()} />
                                </div>

                                {/* Tags */}
                                {tags.length > 0 && (
                                    <div>
                                        <label className="block text-xs font-medium text-muted mb-1">Tags</label>
                                        <div className="flex flex-wrap gap-1">
                                            {tags.map(tag => (
                                                <span key={tag} className="px-2 py-0.5 text-xs bg-primary-50 text-primary-700 rounded-full">
                                                    {tag}
                                                </span>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {/* Skill Structure Tree */}
                                <div>
                                    <label className="block text-xs font-medium text-muted mb-2">Skill Structure</label>
                                    <div className="bg-surface-alt border border-default rounded-lg p-4 font-mono text-sm">
                                        {/* Root: skill name */}
                                        <div className="flex items-center space-x-2 text-default font-semibold">
                                            <FolderTree className="w-4 h-4 text-primary-600" />
                                            <span>{skill.name}/</span>
                                        </div>

                                        {/* SKILL.md */}
                                        <div className="ml-5 mt-1.5 flex items-center space-x-2">
                                            <span className="text-disabled">├──</span>
                                            <FileCode className="w-3.5 h-3.5 text-blue-500" />
                                            <button
                                                onClick={() => setActiveTab('content')}
                                                className="text-blue-600 hover:text-blue-800 hover:underline text-xs"
                                            >
                                                SKILL.md
                                            </button>
                                            <span className="text-xs text-muted">
                                                ({skill.skill_md_content ? `${skill.skill_md_content.split('\n').length} lines` : 'not loaded'})
                                            </span>
                                        </div>

                                        {/* Bundled files — reconstruct the real directory tree from
                                            the stored root-relative paths so scripts/, references/,
                                            assets/, templates/, and any other shipped dirs render at
                                            their true depth (no scripts/scripts duplication, no
                                            templates-under-assets). */}
                                        {(loadingScripts || loadingResources || loadingAssets) ? (
                                            <div className="ml-5 mt-1 flex items-center space-x-2">
                                                <span className="text-disabled">└──</span>
                                                <Loader2 className="w-3 h-3 text-muted animate-spin" />
                                                <span className="text-xs text-muted">Loading bundled files…</span>
                                            </div>
                                        ) : (scripts.length + resources.length + assets.length) === 0 ? (
                                            <div className="ml-5 mt-1 flex items-center space-x-2">
                                                <span className="text-disabled">└──</span>
                                                <span className="text-xs text-muted italic">No bundled resources</span>
                                            </div>
                                        ) : (
                                            <div className="ml-5 mt-1">
                                                <SkillTreeRows
                                                    dir={buildSkillFileTree([
                                                        ...scripts.map((s) => ({ id: s.id, path: s.filename, kind: 'script' as const, meta: s.language, tab: 'scripts' as const })),
                                                        ...resources.map((r) => ({ id: r.id, path: r.filename, kind: 'reference' as const, meta: r.resource_type, tab: 'resources' as const })),
                                                        ...assets.map((a) => ({ id: a.id, path: a.filename, kind: 'asset' as const, meta: a.asset_type, tab: 'assets' as const })),
                                                    ])}
                                                    depth={0}
                                                    onOpenTab={(tab) => setActiveTab(tab)}
                                                />
                                            </div>
                                        )}
                                    </div>
                                </div>

                                {/* Metadata */}
                                <div className="grid grid-cols-3 gap-3">
                                    <MetricCard icon={DollarSign} label="Cost Est."
                                                value={skill.metadata?.cost_estimate_usd != null ? `$${skill.metadata.cost_estimate_usd.toFixed(2)}` : '—'} />
                                    <MetricCard icon={ShieldCheck} label="Approval"
                                                value={skill.metadata?.approval_required ? 'Required' : 'Auto'} />
                                    <MetricCard icon={Layers} label="Rate Tier"
                                                value={skill.metadata?.rate_limit_tier || 'standard'} />
                                </div>

                                {/* Allowed Tools */}
                                {skill.allowed_tools && skill.allowed_tools.length > 0 && (
                                    <div>
                                        <label className="block text-xs font-medium text-muted mb-1">Allowed Tools</label>
                                        <div className="flex flex-wrap gap-1">
                                            {skill.allowed_tools.map(tool => (
                                                <code key={tool} className="px-2 py-0.5 text-xs bg-surface-raised text-secondary rounded">{tool}</code>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {/* Resource Dependencies */}
                                {(skill.metadata?.required_mcp_servers?.length || skill.metadata?.required_llm_models?.length) && (
                                    <div>
                                        <label className="block text-xs font-medium text-muted mb-2">Resource Dependencies</label>
                                        <div className="space-y-1">
                                            {skill.metadata?.required_mcp_servers?.map(id => (
                                                <div key={id} className="flex items-center space-x-2 text-xs">
                                                    <span className="text-muted">MCP Server:</span>
                                                    <code className="px-1.5 py-0.5 bg-surface-raised rounded">{id}</code>
                                                </div>
                                            ))}
                                            {skill.metadata?.required_llm_models?.map(id => (
                                                <div key={id} className="flex items-center space-x-2 text-xs">
                                                    <span className="text-muted">LLM Model:</span>
                                                    <code className="px-1.5 py-0.5 bg-surface-raised rounded">{id}</code>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {/* Group Access (read-only summary) */}
                                <div>
                                    <label className="block text-xs font-medium text-muted mb-1">Group Access</label>
                                    <GroupTreeDisplay groups={skill.groups || []} variant="expanded" maxVisible={10} />
                                </div>

                                {/* Validation Errors */}
                                {skill.validation_errors && skill.validation_errors.length > 0 && (
                                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg">
                                        <h4 className="text-xs font-medium text-red-800 mb-1">Validation Errors</h4>
                                        <ul className="list-disc list-inside text-xs text-red-700 space-y-0.5">
                                            {skill.validation_errors.map((err, i) => <li key={i}>{err}</li>)}
                                        </ul>
                                    </div>
                                )}

                                {/* Deprecation Notice */}
                                {isDeprecated && skill.deprecation_reason && (
                                    <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg">
                                        <h4 className="text-xs font-medium text-amber-800 mb-1">Deprecation Notice</h4>
                                        <p className="text-xs text-amber-700">{skill.deprecation_reason}</p>
                                        {skill.deprecated_at && (
                                            <p className="text-xs text-amber-600 mt-1">Deprecated: {new Date(skill.deprecated_at).toLocaleString()}</p>
                                        )}
                                    </div>
                                )}

                                {/* Timestamps */}
                                <div className="pt-4 border-t border-default grid grid-cols-2 gap-3 text-xs text-muted">
                                    <div><span className="font-medium">Created:</span> {skill.created_at ? new Date(skill.created_at).toLocaleString() : '—'}</div>
                                    <div><span className="font-medium">Updated:</span> {skill.updated_at ? new Date(skill.updated_at).toLocaleString() : '—'}</div>
                                    <div><span className="font-medium">Created by:</span> {skill.created_by || '—'}</div>
                                    {skill.published_at && (
                                        <div><span className="font-medium">Published:</span> {new Date(skill.published_at).toLocaleString()}</div>
                                    )}
                                </div>
                            </div>
                        )}

                        {/* ============= SKILL.md TAB ============= */}
                        {activeTab === 'content' && (
                            <div className="p-6 h-full app-column">
                                {canEdit ? (
                                    <div className="bg-surface border border-default rounded-lg flex flex-col flex-1 min-h-0 min-w-0">
                                        <div className="flex items-center justify-between px-4 py-2 border-b border-default flex-shrink-0">
                                            <div className="flex items-center space-x-2">
                                                <FileCode className="w-4 h-4 text-muted" />
                                                <span className="text-sm font-medium text-secondary">SKILL.md</span>
                                            </div>
                                            <span className="text-xs text-muted">{skillMdContent.length.toLocaleString()} chars</span>
                                        </div>
                                        <textarea
                                            value={skillMdContent}
                                            onChange={(e) => setSkillMdContent(e.target.value)}
                                            spellCheck={false}
                                            className="p-4 text-sm font-mono text-default w-full flex-1 leading-relaxed bg-transparent focus:outline-none resize-none min-h-0 min-w-0"
                                            placeholder="---&#10;name: my-skill&#10;description: ...&#10;---&#10;&#10;# My Skill"
                                        />
                                        {!isCreating && onSave && (
                                            <div className="flex justify-end px-4 py-2 border-t border-default">
                                                <button
                                                    onClick={asyncHandler(async () => {
                                                        if (!skillMdContent.trim()) {
                                                            toast.error('SKILL.md cannot be empty');
                                                            return;
                                                        }
                                                        setSavingEdits(true);
                                                        try {
                                                            await onSave({ skill_md_content: skillMdContent });
                                                            toast.success('SKILL.md saved');
                                                        } catch (e) {
                                                            const msg = e instanceof Error ? e.message : String(e);
                                                            toast.error(`Save failed: ${msg}`);
                                                        } finally {
                                                            setSavingEdits(false);
                                                        }
                                                    }, 'click')}
                                                    disabled={savingEdits}
                                                    className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 rounded-md"
                                                >
                                                    {savingEdits ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                                                    <span>Save SKILL.md</span>
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                ) : skill.skill_md_content ? (
                                    <div className="bg-surface-alt border border-default rounded-lg flex flex-col flex-1 min-h-0 min-w-0">
                                        <div className="flex items-center justify-between px-4 py-2 border-b border-default flex-shrink-0">
                                            <div className="flex items-center space-x-2">
                                                <FileCode className="w-4 h-4 text-muted" />
                                                <span className="text-sm font-medium text-secondary">SKILL.md</span>
                                            </div>
                                            <span className="text-xs text-muted">{skill.skill_md_content.length.toLocaleString()} chars</span>
                                        </div>
                                        <pre className="p-4 text-sm font-mono text-default whitespace-pre-wrap overflow-auto flex-1 leading-relaxed min-h-0 min-w-0">
                                            {skill.skill_md_content}
                                        </pre>
                                    </div>
                                ) : (
                                    <div className="text-center py-12 text-muted">
                                        <FileCode className="w-12 h-12 mx-auto mb-3 text-disabled" />
                                        <p className="text-sm">SKILL.md content not loaded.</p>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= RESOURCES TAB ============= */}
                        {activeTab === 'resources' && (
                            <div className="p-6">
                                <div className="flex items-center justify-between mb-4 max-w-4xl">
                                    <div>
                                        <h3 className="text-sm font-medium text-default">Resources</h3>
                                        <p className="text-xs text-muted mt-0.5">Reference documents and files in the skill's <code className="px-1 py-0.5 bg-surface-raised rounded text-xs">references/</code> directory.</p>
                                    </div>
                                    {canEdit && createResource && (
                                        <button onClick={() => { resetResourceForm(); setShowResourceForm(true); }}
                                                className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors">
                                            <Plus className="w-3.5 h-3.5" /><span>Add Resource</span>
                                        </button>
                                    )}
                                </div>

                                {/* Inline form */}
                                {showResourceForm && canEdit && (
                                    <div className="border border-primary-200 rounded-lg p-4 bg-primary-50/30 space-y-3 mb-4 max-w-4xl">
                                        <h4 className="text-sm font-medium text-default">{editingResource ? `Edit: ${editingResource.filename}` : 'New Resource'}</h4>
                                        <div className="grid grid-cols-2 gap-3">
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Filename *</label>
                                                <input type="text" value={resourceForm.filename} disabled={!!editingResource}
                                                       onChange={(e) => setResourceForm(f => ({ ...f, filename: e.target.value }))}
                                                       placeholder="REFERENCE.md"
                                                       className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 disabled:bg-surface-raised" />
                                            </div>
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Type</label>
                                                <select value={resourceForm.resource_type} onChange={(e) => setResourceForm(f => ({ ...f, resource_type: e.target.value }))}
                                                        className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500">
                                                    {RESOURCE_TYPES.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
                                                </select>
                                            </div>
                                        </div>
                                        <div>
                                            <label className="block text-xs font-medium text-secondary mb-1">MIME Type</label>
                                            <input type="text" value={resourceForm.mime_type} onChange={(e) => setResourceForm(f => ({ ...f, mime_type: e.target.value }))}
                                                   placeholder="text/markdown, application/json, etc."
                                                   className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500" />
                                        </div>
                                        <div>
                                            <label className="block text-xs font-medium text-secondary mb-1">Content *</label>
                                            <textarea value={resourceForm.content} onChange={(e) => setResourceForm(f => ({ ...f, content: e.target.value }))}
                                                      rows={10} placeholder="Paste file content here..."
                                                      className="w-full px-3 py-2 text-sm font-mono border border-strong rounded-lg focus:ring-2 focus:ring-primary-500" />
                                        </div>
                                        <div className="flex items-center space-x-2">
                                            <button onClick={asyncHandler(handleResourceSave, 'click')} className="flex items-center space-x-1.5 px-4 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700">
                                                <CheckCircle className="w-3.5 h-3.5" /><span>{editingResource ? 'Update' : 'Create'}</span>
                                            </button>
                                            <button onClick={resetResourceForm} className="px-4 py-1.5 text-sm text-secondary hover:bg-surface-raised rounded-lg">Cancel</button>
                                        </div>
                                    </div>
                                )}

                                <ReadFailureNotice failure={readFailure('resources')} onRetry={askAgain} />
                                {loadingResources ? (
                                    <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-primary-600 animate-spin" /></div>
                                ) : resources.length > 0 ? (
                                    <div className="space-y-2 max-w-4xl">
                                        {resources.map(res => {
                                            const full = loadedContent[res.id];
                                            const isExpanded = expandedResourceId === res.id;
                                            return (
                                                <div key={res.id} className="border border-default rounded-lg bg-surface">
                                                    <div className="flex items-center">
                                                        <button onClick={() => handleExpandResource(res.id)}
                                                                className="flex-1 px-4 py-3 flex items-center justify-between hover:bg-surface-alt transition-colors text-left min-h-0 min-w-0">
                                                            <div className="flex items-center space-x-3 min-w-0">
                                                                <FolderOpen className="w-4 h-4 text-muted flex-shrink-0" />
                                                                <div className="min-w-0">
                                                                    <p className="text-sm font-medium text-default truncate">{res.filename}</p>
                                                                    <p className="text-xs text-muted">{res.resource_type}{res.size_bytes != null && ` • ${(res.size_bytes / 1024).toFixed(1)} KB`}</p>
                                                                </div>
                                                            </div>
                                                            {isExpanded ? <ChevronDown className="w-4 h-4 text-muted" /> : <ChevronRight className="w-4 h-4 text-muted" />}
                                                        </button>
                                                        {canEdit && (
                                                            <div className="flex items-center space-x-0.5 pr-3 flex-shrink-0">
                                                                <button onClick={asyncHandler(() => handleResourceEdit(res), 'click')} className="p-1.5 text-muted hover:text-primary-600 hover:bg-surface-raised rounded" title="Edit"><Edit className="w-3.5 h-3.5" /></button>
                                                                <button onClick={asyncHandler(() => handleResourceDelete(res), 'click')} className="p-1.5 text-muted hover:text-red-600 hover:bg-red-50 rounded" title="Delete"><Trash2 className="w-3.5 h-3.5" /></button>
                                                            </div>
                                                        )}
                                                    </div>
                                                    {isExpanded && (
                                                        <div className="px-4 pb-4 border-t border-default pt-3 space-y-3">
                                                            {loadingContentId === res.id ? (
                                                                <div className="flex items-center space-x-2 py-4 justify-center text-muted"><Loader2 className="w-4 h-4 animate-spin" /><span className="text-xs">Loading content…</span></div>
                                                            ) : full ? (
                                                                <>
                                                                    <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                                                                        <div><span className="font-medium">Type:</span> {full.resource_type || '—'}</div>
                                                                        <div><span className="font-medium">MIME:</span> {full.mime_type || '—'}</div>
                                                                        <div><span className="font-medium">Size:</span> {full.size_bytes != null ? `${(full.size_bytes / 1024).toFixed(1)} KB` : '—'}</div>
                                                                    </div>
                                                                    {full.content && (
                                                                        <div>
                                                                            <label className="text-xs font-medium text-muted block mb-1">Content</label>
                                                                            <pre className="bg-surface-alt border border-default rounded-lg p-3 text-xs font-mono overflow-auto max-h-80 whitespace-pre-wrap">{full.content}</pre>
                                                                        </div>
                                                                    )}
                                                                </>
                                                            ) : (
                                                                <p className="text-xs text-muted text-center py-2">Content not available</p>
                                                            )}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                ) : (
                                    <div className="text-center py-16 text-muted">
                                        <FolderOpen className="w-12 h-12 mx-auto mb-3 text-disabled" />
                                        <p className="text-sm font-medium">No Resources</p>
                                        <p className="text-xs mt-1">This skill has no attached resource files.</p>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= SCRIPTS TAB ============= */}
                        {activeTab === 'scripts' && (
                            <div className="p-6">
                                <div className="flex items-center justify-between mb-4 max-w-4xl">
                                    <div>
                                        <h3 className="text-sm font-medium text-default">Scripts</h3>
                                        <p className="text-xs text-muted mt-0.5">Executable scripts bundled with this skill.</p>
                                    </div>
                                    {canEdit && createScript && (
                                        <button onClick={() => { resetScriptForm(); setShowScriptForm(true); }}
                                                className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors">
                                            <Plus className="w-3.5 h-3.5" /><span>Add Script</span>
                                        </button>
                                    )}
                                </div>

                                {showScriptForm && canEdit && (
                                    <div className="border border-primary-200 rounded-lg p-4 bg-primary-50/30 space-y-3 mb-4 max-w-4xl">
                                        <h4 className="text-sm font-medium text-default">{editingScript ? `Edit: ${editingScript.filename}` : 'New Script'}</h4>
                                        <div className="grid grid-cols-2 gap-3">
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Filename *</label>
                                                <input type="text" value={scriptForm.filename} disabled={!!editingScript}
                                                       onChange={(e) => setScriptForm(f => ({ ...f, filename: e.target.value }))}
                                                       placeholder="run.py"
                                                       className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 disabled:bg-surface-raised" />
                                            </div>
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Description</label>
                                                <input type="text" value={scriptForm.description}
                                                       onChange={(e) => setScriptForm(f => ({ ...f, description: e.target.value }))}
                                                       placeholder="Main execution script"
                                                       className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500" />
                                            </div>
                                        </div>
                                        <div className="grid grid-cols-2 gap-3">
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Language</label>
                                                <select value={scriptForm.language} onChange={(e) => setScriptForm(f => ({ ...f, language: e.target.value }))}
                                                        className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500">
                                                    {SCRIPT_LANGUAGES.map(l => <option key={l} value={l}>{l}</option>)}
                                                </select>
                                            </div>
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Execution Mode</label>
                                                <select value={scriptForm.execution_mode} onChange={(e) => setScriptForm(f => ({ ...f, execution_mode: e.target.value }))}
                                                        className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500">
                                                    {SCRIPT_EXECUTION_MODES.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                                                </select>
                                            </div>
                                        </div>
                                        <div>
                                            <label className="block text-xs font-medium text-secondary mb-1">Script Content *</label>
                                            <textarea value={scriptForm.script_content} onChange={(e) => setScriptForm(f => ({ ...f, script_content: e.target.value }))}
                                                      rows={12} placeholder="Paste script content here..."
                                                      className="w-full px-3 py-2 text-sm font-mono border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 bg-gray-900 text-gray-100" />
                                        </div>
                                        <div className="flex items-center space-x-2">
                                            <button onClick={asyncHandler(handleScriptSave, 'click')} className="flex items-center space-x-1.5 px-4 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700">
                                                <CheckCircle className="w-3.5 h-3.5" /><span>{editingScript ? 'Update' : 'Create'}</span>
                                            </button>
                                            <button onClick={resetScriptForm} className="px-4 py-1.5 text-sm text-secondary hover:bg-surface-raised rounded-lg">Cancel</button>
                                        </div>
                                    </div>
                                )}

                                <ReadFailureNotice failure={readFailure('scripts')} onRetry={askAgain} />
                                {loadingScripts ? (
                                    <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-primary-600 animate-spin" /></div>
                                ) : scripts.length > 0 ? (
                                    <div className="space-y-2 max-w-4xl">
                                        {scripts.map(scr => {
                                            const full = loadedContent[scr.id];
                                            const isExpanded = expandedScriptId === scr.id;
                                            return (
                                                <div key={scr.id} className="border border-default rounded-lg bg-surface">
                                                    <div className="flex items-center">
                                                        <button onClick={() => handleExpandScript(scr.id)}
                                                                className="flex-1 px-4 py-3 flex items-center justify-between hover:bg-surface-alt transition-colors text-left min-h-0 min-w-0">
                                                            <div className="flex items-center space-x-3 min-w-0">
                                                                <Code2 className="w-4 h-4 text-muted flex-shrink-0" />
                                                                <div className="min-w-0">
                                                                    <p className="text-sm font-medium text-default truncate">{scr.filename}</p>
                                                                    <p className="text-xs text-muted">{scr.language} • {scr.execution_mode}</p>
                                                                </div>
                                                            </div>
                                                            {isExpanded ? <ChevronDown className="w-4 h-4 text-muted" /> : <ChevronRight className="w-4 h-4 text-muted" />}
                                                        </button>
                                                        {canEdit && (
                                                            <div className="flex items-center space-x-0.5 pr-3 flex-shrink-0">
                                                                <button onClick={asyncHandler(() => handleScriptEdit(scr), 'click')} className="p-1.5 text-muted hover:text-primary-600 hover:bg-surface-raised rounded" title="Edit"><Edit className="w-3.5 h-3.5" /></button>
                                                                <button onClick={asyncHandler(() => handleScriptDelete(scr), 'click')} className="p-1.5 text-muted hover:text-red-600 hover:bg-red-50 rounded" title="Delete"><Trash2 className="w-3.5 h-3.5" /></button>
                                                            </div>
                                                        )}
                                                    </div>
                                                    {isExpanded && (
                                                        <div className="px-4 pb-4 border-t border-default pt-3 space-y-3">
                                                            {loadingContentId === scr.id ? (
                                                                <div className="flex items-center space-x-2 py-4 justify-center text-muted"><Loader2 className="w-4 h-4 animate-spin" /><span className="text-xs">Loading content…</span></div>
                                                            ) : full ? (
                                                                <>
                                                                    {full.description && <p className="text-xs text-secondary">{full.description}</p>}
                                                                    <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                                                                        <div><span className="font-medium">Language:</span> {full.language || '—'}</div>
                                                                        <div><span className="font-medium">Mode:</span> {full.execution_mode || '—'}</div>
                                                                        <div><span className="font-medium">Size:</span> {full.size_bytes != null ? `${(full.size_bytes / 1024).toFixed(1)} KB` : '—'}</div>
                                                                    </div>
                                                                    {full.dependencies && full.dependencies.length > 0 && (
                                                                        <div>
                                                                            <label className="text-xs font-medium text-muted block mb-1">Dependencies</label>
                                                                            <div className="flex flex-wrap gap-1">
                                                                                {full.dependencies.map((dep: string, i: number) => (
                                                                                    <code key={i} className="px-2 py-0.5 bg-surface-raised text-secondary rounded text-xs">{dep}</code>
                                                                                ))}
                                                                            </div>
                                                                        </div>
                                                                    )}
                                                                    {full.script_content && (
                                                                        <div>
                                                                            <label className="text-xs font-medium text-muted block mb-1">Source</label>
                                                                            <pre className="bg-gray-900 text-gray-100 rounded-lg p-4 text-xs font-mono overflow-auto max-h-[50vh] whitespace-pre-wrap leading-relaxed">{full.script_content}</pre>
                                                                        </div>
                                                                    )}
                                                                </>
                                                            ) : (
                                                                <p className="text-xs text-muted text-center py-2">Content not available</p>
                                                            )}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                ) : (
                                    <div className="text-center py-16 text-muted">
                                        <Code2 className="w-12 h-12 mx-auto mb-3 text-disabled" />
                                        <p className="text-sm font-medium">No Scripts</p>
                                        <p className="text-xs mt-1">This skill has no attached scripts.</p>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= ASSETS TAB ============= */}
                        {activeTab === 'assets' && (
                            <div className="p-6">
                                <div className="flex items-center justify-between mb-4 max-w-4xl">
                                    <div>
                                        <h3 className="text-sm font-medium text-default">Assets</h3>
                                        <p className="text-xs text-muted mt-0.5">Data files, templates, and other assets bundled with this skill.</p>
                                    </div>
                                    {canEdit && createAsset && (
                                        <button onClick={() => { resetAssetForm(); setShowAssetForm(true); }}
                                                className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors">
                                            <Plus className="w-3.5 h-3.5" /><span>Add Asset</span>
                                        </button>
                                    )}
                                </div>

                                {showAssetForm && canEdit && (
                                    <div className="border border-primary-200 rounded-lg p-4 bg-primary-50/30 space-y-3 mb-4 max-w-4xl">
                                        <h4 className="text-sm font-medium text-default">{editingAsset ? `Edit: ${editingAsset.filename}` : 'New Asset'}</h4>
                                        <div className="grid grid-cols-2 gap-3">
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Filename *</label>
                                                <input type="text" value={assetForm.filename} disabled={!!editingAsset}
                                                       onChange={(e) => setAssetForm(f => ({ ...f, filename: e.target.value }))}
                                                       placeholder="data.json"
                                                       className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 disabled:bg-surface-raised" />
                                            </div>
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Description</label>
                                                <input type="text" value={assetForm.description}
                                                       onChange={(e) => setAssetForm(f => ({ ...f, description: e.target.value }))}
                                                       placeholder="Configuration data"
                                                       className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500" />
                                            </div>
                                        </div>
                                        <div className="grid grid-cols-3 gap-3">
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Asset Type</label>
                                                <select value={assetForm.asset_type} onChange={(e) => setAssetForm(f => ({ ...f, asset_type: e.target.value }))}
                                                        className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500">
                                                    {ASSET_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
                                                </select>
                                            </div>
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">MIME Type</label>
                                                <input type="text" value={assetForm.mime_type} onChange={(e) => setAssetForm(f => ({ ...f, mime_type: e.target.value }))}
                                                       placeholder="application/json"
                                                       className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500" />
                                            </div>
                                            <div>
                                                <label className="block text-xs font-medium text-secondary mb-1">Encoding</label>
                                                <select value={assetForm.encoding} onChange={(e) => setAssetForm(f => ({ ...f, encoding: e.target.value }))}
                                                        className="w-full px-3 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500">
                                                    {ASSET_ENCODINGS.map(e => <option key={e.value} value={e.value}>{e.label}</option>)}
                                                </select>
                                            </div>
                                        </div>
                                        <div>
                                            <label className="block text-xs font-medium text-secondary mb-1">Content *</label>
                                            <textarea value={assetForm.content} onChange={(e) => setAssetForm(f => ({ ...f, content: e.target.value }))}
                                                      rows={10} placeholder="Paste asset content here..."
                                                      className="w-full px-3 py-2 text-sm font-mono border border-strong rounded-lg focus:ring-2 focus:ring-primary-500" />
                                        </div>
                                        <div className="flex items-center space-x-2">
                                            <button onClick={asyncHandler(handleAssetSave, 'click')} className="flex items-center space-x-1.5 px-4 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700">
                                                <CheckCircle className="w-3.5 h-3.5" /><span>{editingAsset ? 'Update' : 'Create'}</span>
                                            </button>
                                            <button onClick={resetAssetForm} className="px-4 py-1.5 text-sm text-secondary hover:bg-surface-raised rounded-lg">Cancel</button>
                                        </div>
                                    </div>
                                )}

                                <ReadFailureNotice failure={readFailure('assets')} onRetry={askAgain} />
                                {loadingAssets ? (
                                    <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-primary-600 animate-spin" /></div>
                                ) : assets.length > 0 ? (
                                    <div className="space-y-2 max-w-4xl">
                                        {assets.map(asset => {
                                            const full = loadedContent[asset.id];
                                            const isExpanded = expandedAssetId === asset.id;
                                            return (
                                                <div key={asset.id} className="border border-default rounded-lg bg-surface">
                                                    <div className="flex items-center">
                                                        <button onClick={() => handleExpandAsset(asset.id)}
                                                                className="flex-1 px-4 py-3 flex items-center justify-between hover:bg-surface-alt transition-colors text-left min-h-0 min-w-0">
                                                            <div className="flex items-center space-x-3 min-w-0">
                                                                <Package className="w-4 h-4 text-muted flex-shrink-0" />
                                                                <div className="min-w-0">
                                                                    <p className="text-sm font-medium text-default truncate">{asset.filename}</p>
                                                                    <p className="text-xs text-muted">{asset.asset_type}{asset.size_bytes != null && ` • ${(asset.size_bytes / 1024).toFixed(1)} KB`}</p>
                                                                </div>
                                                            </div>
                                                            {isExpanded ? <ChevronDown className="w-4 h-4 text-muted" /> : <ChevronRight className="w-4 h-4 text-muted" />}
                                                        </button>
                                                        {canEdit && (
                                                            <div className="flex items-center space-x-0.5 pr-3 flex-shrink-0">
                                                                <button onClick={asyncHandler(() => handleAssetEdit(asset), 'click')} className="p-1.5 text-muted hover:text-primary-600 hover:bg-surface-raised rounded" title="Edit"><Edit className="w-3.5 h-3.5" /></button>
                                                                <button onClick={asyncHandler(() => handleAssetDelete(asset), 'click')} className="p-1.5 text-muted hover:text-red-600 hover:bg-red-50 rounded" title="Delete"><Trash2 className="w-3.5 h-3.5" /></button>
                                                            </div>
                                                        )}
                                                    </div>
                                                    {isExpanded && (
                                                        <div className="px-4 pb-4 border-t border-default pt-3 space-y-3">
                                                            {loadingContentId === asset.id ? (
                                                                <div className="flex items-center space-x-2 py-4 justify-center text-muted"><Loader2 className="w-4 h-4 animate-spin" /><span className="text-xs">Loading content…</span></div>
                                                            ) : full ? (
                                                                <>
                                                                    {full.description && <p className="text-xs text-secondary">{full.description}</p>}
                                                                    <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                                                                        <div><span className="font-medium">Type:</span> {full.asset_type || '—'}</div>
                                                                        <div><span className="font-medium">MIME:</span> {full.mime_type || '—'}</div>
                                                                        <div><span className="font-medium">Encoding:</span> {full.encoding || '—'}</div>
                                                                    </div>
                                                                    {full.content && (
                                                                        <div>
                                                                            <label className="text-xs font-medium text-muted block mb-1">Content</label>
                                                                            <pre className="bg-surface-alt border border-default rounded-lg p-3 text-xs font-mono overflow-auto max-h-80 whitespace-pre-wrap">{full.content}</pre>
                                                                        </div>
                                                                    )}
                                                                </>
                                                            ) : (
                                                                <p className="text-xs text-muted text-center py-2">Content not available</p>
                                                            )}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                ) : (
                                    <div className="text-center py-16 text-muted">
                                        <Package className="w-12 h-12 mx-auto mb-3 text-disabled" />
                                        <p className="text-sm font-medium">No Assets</p>
                                        <p className="text-xs mt-1">This skill has no attached asset files.</p>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= GROUPS TAB (Editable) ============= */}
                        {activeTab === 'groups' && (
                            <div className="p-6">
                                {loadingGroupTree ? (
                                    <div className="flex items-center justify-center py-12">
                                        <Loader2 className="w-6 h-6 text-primary-600 animate-spin" />
                                    </div>
                                ) : readFailure('groups') && groupTree.length === 0 ? (
                                    // Without the tree the selection is not the skill's: nothing is offered to save.
                                    <ReadFailureNotice failure={readFailure('groups')} onRetry={askAgain} />
                                ) : fetchGroupTree && onUpdateGroups ? (
                                    <div className="space-y-4 max-w-3xl">
                                        <div className="flex items-center justify-between">
                                            <div>
                                                <h3 className="text-sm font-medium text-default">Group Access</h3>
                                                <p className="text-xs text-muted mt-0.5">
                                                    Select which groups can access this skill. Changes require saving.
                                                </p>
                                            </div>
                                            <button
                                                onClick={asyncHandler(handleSaveGroups, 'click')}
                                                disabled={!groupsDirty || savingGroups}
                                                className={`px-4 py-2 text-sm font-medium rounded-lg flex items-center space-x-2 transition-colors ${
                                                    groupsDirty
                                                        ? 'bg-primary-600 text-white hover:bg-primary-700'
                                                        : 'bg-surface-raised text-muted cursor-not-allowed'
                                                }`}
                                            >
                                                {savingGroups ? (
                                                    <Loader2 className="w-4 h-4 animate-spin" />
                                                ) : (
                                                    <Save className="w-4 h-4" />
                                                )}
                                                <span>Save Changes</span>
                                            </button>
                                        </div>

                                        {groupsDirty && (
                                            <div className="px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-700 flex items-center space-x-2">
                                                <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
                                                <span>You have unsaved group access changes.</span>
                                            </div>
                                        )}

                                        <HierarchicalGroupSelector
                                            groupTree={groupTree}
                                            selectedGroupIds={selectedGroupIds}
                                            onSelectionChange={handleGroupSelectionChange}
                                        />
                                    </div>
                                ) : (
                                    <div>
                                        <h3 className="text-sm font-medium text-default mb-3">Group Access</h3>
                                        <GroupTreeDisplay groups={skill.groups || []} variant="expanded" maxVisible={20} />
                                    </div>
                                )}
                            </div>
                        )}

                        {/* ============= LIMITS TAB (tenant-wide) ============= */}
                        {activeTab === 'limits' && (
                            <div className="p-6 space-y-4">
                                <p className="text-sm text-secondary">
                                    Tenant-wide default usage limits for this skill. Each Resource
                                    Group can only tighten them — the stricter value wins.
                                </p>
                                <SkillLimitsForm
                                    value={tenantLimits}
                                    onChange={setTenantLimits}
                                />
                                {storedTenantGovernance.map(([key, value]) => (
                                    <NotInThisRelease
                                        key={key}
                                        testId={`skill-governance-unavailable-${key}`}
                                        label={`Skill policy: ${key.replace(/_/g, ' ')}`}
                                        value={shownValue(value)}
                                        consequence="It is kept as it is; a Resource Group's skill governance applies."
                                    />
                                ))}
                                <div className="flex justify-end">
                                    <button
                                        type="button"
                                        onClick={asyncHandler(handleSaveLimits, 'click')}
                                        disabled={savingEdits}
                                        className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50"
                                    >
                                        {savingEdits ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                                        Save Limits
                                    </button>
                                </div>
                            </div>
                        )}

                        {/* ============= EXECUTIONS TAB ============= */}
                        {activeTab === 'executions' && (
                            <div className="p-6">
                                <ReadFailureNotice failure={readFailure('executions')} onRetry={askAgain} />
                                {loadingExecutions ? (
                                    <div className="flex items-center justify-center py-12">
                                        <Loader2 className="w-6 h-6 text-primary-600 animate-spin" />
                                    </div>
                                ) : executions.length > 0 ? (
                                    <div className="space-y-2 max-w-4xl">
                                        <p className="text-xs text-muted mb-3">
                                            Showing {executions.length} of {executionTotal} execution{executionTotal !== 1 ? 's' : ''}
                                        </p>
                                        {executions.map(exec => (
                                            <ExecutionCard key={exec.id} execution={exec} />
                                        ))}
                                    </div>
                                ) : (
                                    <div className="text-center py-16 text-muted">
                                        <Activity className="w-12 h-12 mx-auto mb-3 text-disabled" />
                                        <p className="text-sm font-medium">No Executions</p>
                                        <p className="text-xs mt-1">This skill has not been executed yet.</p>
                                    </div>
                                )}
                            </div>
                        )}

                    </div>
                </div>
            </div>
        </div>
    );
};

// ============================================================================
// Helper Components
// ============================================================================

const InfoField: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono }) => (
    <div>
        <label className="block text-xs font-medium text-muted">{label}</label>
        <p className={`text-sm text-default mt-0.5 ${mono ? 'font-mono' : ''}`}>{value}</p>
    </div>
);

const MetricCard: React.FC<{ icon: React.FC<{ className?: string }>; label: string; value: string }> = ({ icon: Icon, label, value }) => (
    <div className="bg-surface-alt border border-default rounded-lg p-3 text-center">
        <Icon className="w-4 h-4 text-muted mx-auto mb-1" />
        <p className="text-xs text-muted">{label}</p>
        <p className="text-sm font-medium text-default mt-0.5">{value}</p>
    </div>
);

export default AgentSkillDetailPanel;