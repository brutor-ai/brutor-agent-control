/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compile-time parity between the hand-written composition types in
 * `aiSystemTypes.ts` and the Control Plane's generated OpenAPI schemas.
 *
 * The hand types exist because the generated ones mark every defaulted
 * Pydantic field optional (`id?: string | null`), which the server always
 * sends; rendering code would otherwise be `?? null` noise. The price of a
 * hand type is drift, so this file pins it: a field renamed, added or
 * removed on either side fails `tsc` here, naming the schema. Regenerate
 * with `npm run gen:api-types:control`.
 *
 * Type-only: nothing here exists at runtime.
 */

import type { components } from '../generated/control-plane-api';
import type { CreateResourceGroupRequest, ResourceGroup, UpdateResourceGroupRequest } from '../groupTypes';
import type { BoardSystemRef, SystemObligationsResponse } from '../obligationTypes';
import type {
    AgentImplementationDeclaration,
    AgentRelease,
    AgentReleaseListResponse,
    AgentReleaseRef,
    AgentReleaseTrust,
} from '../agentIdentityTypes';
import type {
    AISystemComposition,
    AISystemEvidenceItem,
    DependencyAssumptions,
    DependencyEvidenceProvided,
    DependencySealStatus,
    EvidenceCreateRequest,
    SeparabilityDeclaration,
    BoundaryMetrics,
    DependencyCap,
    DependencyHealthEntry,
    FallbackCheck,
    SystemHealth,
    AISystemCompositionView,
    AISystemLivenessState,
    CompositionConnection,
    AISystemMemberCounts,
    AISystemSummary,
    CompositionAgent,
    CompositionClientLabel,
    CompositionDependency,
    CompositionDependent,
    CompositionFinding,
    CompositionLimit,
    CompositionLocationStep,
    CompositionPolicy,
    CompositionMcpResource,
    CompositionResource,
    CompositionSystemFacts,
    LivenessStatus,
    DependencyDraft,
    DependencyEdge,
    DependencyListResponse,
    DependencyObserved,
    EffectiveTierSource,
    ObservedUndeclared,
    LifecycleResponse,
    RunDetail,
    RunSummary,
    Baseline,
    DriftEvent,
} from '../aiSystemTypes';
import type {
    BaselineEpoch,
    BaselineEpochListResponse,
    CloseDriftRequest,
    DriftResolution,
    EpochKind,
    HumanResolutionKind,
    IncidentCauseCategory,
    IncidentResourceType,
    IncidentWindow,
    IncidentWindowCreate,
    IncidentWindowDeleteResponse,
    IncidentWindowListResponse,
    IncidentWindowUpdate,
    IncidentWindowWriteResponse,
    ResolutionKind,
} from '../driftResolutionTypes';

type S = components['schemas'];

/** `true` when both types have exactly the same keys; otherwise an object
 *  naming the keys only one side has. */
type SameKeys<Hand, Gen> = [Exclude<keyof Hand, keyof Gen>, Exclude<keyof Gen, keyof Hand>] extends [never, never]
    ? true
    : { onlyInHand: Exclude<keyof Hand, keyof Gen>; onlyInGenerated: Exclude<keyof Gen, keyof Hand> };

/** `true` when every generated key exists on the hand type (the hand type
 *  may carry fields the list endpoint also returns). */
type CoversKeys<Hand, Gen> = [Exclude<keyof Gen, keyof Hand>] extends [never]
    ? true
    : { missingInHand: Exclude<keyof Gen, keyof Hand> };

/** `true` when two literal unions are the same set; otherwise names the
 *  values only one side has. Used for the enums the UI switches on. */
type SameValues<Hand, Gen> = [Exclude<Hand, Gen>, Exclude<Gen, Hand>] extends [never, never]
    ? true
    : { onlyInHand: Exclude<Hand, Gen>; onlyInGenerated: Exclude<Gen, Hand> };

export interface CompositionContractParity {
    composition: SameKeys<AISystemCompositionView, S['CompositionResponse']>;
    system: SameKeys<CompositionSystemFacts, S['CompositionSystem']>;
    location: SameKeys<CompositionLocationStep, S['LocationEntry']>;
    resource: SameKeys<CompositionResource, S['ResourceRef']>;
    mcpResource: SameKeys<CompositionMcpResource, S['McpResourceRef']>;
    policy: SameKeys<CompositionPolicy, S['GatewayPolicy']>;
    limit: SameKeys<CompositionLimit, S['GatewayLimit']>;
    dependency: SameKeys<CompositionDependency, S['CompositionDependency']>;
    observed: SameKeys<DependencyObserved, S['ObservedCalls']>;
    observedUndeclared: SameKeys<ObservedUndeclared, S['ObservedUndeclared']>;
    dependent: SameKeys<CompositionDependent, S['Dependent']>;
    clientLabel: SameKeys<CompositionClientLabel, S['ClientLabel']>;
    finding: SameKeys<CompositionFinding, S['CompositionFinding']>;
    edge: SameKeys<DependencyEdge, S['DependencyEdge']>;
    edgeList: SameKeys<DependencyListResponse, S['DependencyListResponse']>;
    draft: SameKeys<DependencyDraft, S['DependencyCreate']>;
    listComposition: SameKeys<AISystemComposition, S['AISystemComposition']>;
    members: SameKeys<AISystemMemberCounts, S['AISystemMembers']>;
    tierSource: SameKeys<EffectiveTierSource, S['AISystemTierSource']>;
    liveness: SameKeys<AISystemLivenessState, S['AISystemLiveness']>;
    /** The liveness endpoint — its `state` is what every surface renders. */
    livenessStatus: SameKeys<LivenessStatus, S['LivenessStatusResponse']>;
    livenessState: SameValues<LivenessStatus['state'], S['LivenessStatusResponse']['state']>;
    summary: CoversKeys<AISystemSummary, S['AISystemSummary']>;
    // The health payload and its dependency section.
    health: SameKeys<SystemHealth, S['HealthResponse']>;
    dependencyHealth: SameKeys<DependencyHealthEntry, S['DependencyHealthEntry']>;
    boundary: SameKeys<BoundaryMetrics, S['BoundaryMetrics']>;
    fallbackCheck: SameKeys<FallbackCheck, S['FallbackCheck']>;
    dependencyCap: SameKeys<DependencyCap, S['DependencyCap']>;
    dependencyStatus: SameValues<DependencyHealthEntry['status'], S['DependencyHealthEntry']['status']>;
    dependencyBasis: SameValues<DependencyHealthEntry['basis'], S['DependencyHealthEntry']['basis']>;
    signalBasis: SameValues<NonNullable<SystemHealth['signal_basis']>, NonNullable<S['HealthResponse']['signal_basis']>>;
    // Assumptions schema, sealed separability, supplier evidence,
    // accepted-caller enforcement and evaluated tiers.
    assumptions: SameKeys<DependencyAssumptions, S['DependencyAssumptions']>;
    evidenceProvided: SameValues<DependencyEvidenceProvided, NonNullable<S['DependencyAssumptions']['evidence_provided']>[number]>;
    separabilityDeclaration: SameKeys<SeparabilityDeclaration, S['SeparabilityDeclaration']>;
    separabilitySide: SameValues<SeparabilityDeclaration['side'], S['SeparabilityDeclaration']['side']>;
    sealStatus: SameKeys<DependencySealStatus, S['SealStatus']>;
    evidence: SameKeys<AISystemEvidenceItem, S['EvidenceResponse']>;
    evidenceCreate: SameKeys<EvidenceCreateRequest, S['EvidenceCreateRequest']>;
    groupEnforce: CoversKeys<ResourceGroup, Pick<S['ResourceGroupResponse'], 'enforce_accepted_callers' | 'intended_client_systems'>>;
    groupCreateEnforce: CoversKeys<CreateResourceGroupRequest, Pick<S['ResourceGroupCreate'], 'enforce_accepted_callers' | 'intended_client_systems'>>;
    groupUpdateEnforce: CoversKeys<UpdateResourceGroupRequest, Pick<S['ResourceGroupUpdate'], 'enforce_accepted_callers' | 'intended_client_systems'>>;
    boardSystem: SameKeys<BoardSystemRef, S['BoardSystemRef']>;
    systemObligations: SameKeys<SystemObligationsResponse, S['SystemObligationsResponse']>;
    obligationTierSource: SameKeys<NonNullable<SystemObligationsResponse['effective_tier_sources']>[number], S['EffectiveTierSource']>;
    // Agent implementation identity.
    compositionAgent: SameKeys<CompositionAgent, S['AgentRef']>;
    agentReleaseRef: SameKeys<AgentReleaseRef, S['AgentReleaseRef']>;
    agentReleaseTrust: SameValues<AgentReleaseTrust, S['AgentReleaseRef']['trust']>;
    agentRelease: SameKeys<AgentRelease, S['AgentReleaseResponse']>;
    agentReleaseTrustTimeline: SameValues<AgentReleaseTrust, S['AgentReleaseResponse']['trust']>;
    agentReleaseList: SameKeys<AgentReleaseListResponse, S['AgentReleaseListResponse']>;
    /** The three declaration fields travel on the identity, its create and
     *  its update (the dialog saves them with the rest of the identity). */
    identityDeclaration: CoversKeys<S['AgentIdentityResponse'], AgentImplementationDeclaration>;
    identityCreateDeclaration: CoversKeys<S['AgentIdentityCreateRequest'], AgentImplementationDeclaration>;
    identityUpdateDeclaration: CoversKeys<S['AgentIdentityUpdateRequest'], AgentImplementationDeclaration>;
    runSummary: CoversKeys<RunSummary, S['RunSummary']>;
    runDetail: CoversKeys<RunDetail, S['RunDetail']>;
    runTrust: SameValues<NonNullable<RunSummary['agent_impl_trust']>, NonNullable<S['RunSummary']['agent_impl_trust']>>;
    lifecycle: CoversKeys<LifecycleResponse, S['LifecycleResponse']>;
    // The Simple graph's connector lines — one status per connection.
    connection: SameKeys<CompositionConnection, S['CompositionConnection']>;
    connectionStatus: SameValues<CompositionConnection['status'], S['CompositionConnection']['status']>;
    // Drift resolution that the detector honours.
    driftEvent: SameKeys<DriftEvent, S['DriftEventResponse']>;
    resolutionKind: SameValues<ResolutionKind, NonNullable<S['DriftEventResponse']['resolution_kind']>>;
    baseline: SameKeys<Baseline, S['BaselineResponse']>;
    baselineEpoch: SameKeys<BaselineEpoch, S['BaselineEpochResponse']>;
    epochKind: SameValues<EpochKind, S['BaselineEpochResponse']['kind']>;
    baselineEpochList: SameKeys<BaselineEpochListResponse, S['BaselineEpochListResponse']>;
    closeDriftRequest: SameKeys<CloseDriftRequest, S['CloseDriftRequest']>;
    humanResolutionKind: SameValues<HumanResolutionKind, S['CloseDriftRequest']['kind']>;
    closeCauseCategory: SameValues<IncidentCauseCategory, NonNullable<S['CloseDriftRequest']['cause_category']>>;
    driftResolution: SameKeys<DriftResolution, S['DriftResolutionResponse']>;
    incidentWindow: SameKeys<IncidentWindow, S['IncidentWindowResponse']>;
    incidentCauseCategory: SameValues<IncidentCauseCategory, S['IncidentWindowResponse']['cause_category']>;
    incidentResourceType: SameValues<IncidentResourceType, NonNullable<S['IncidentWindowResponse']['resource_type']>>;
    incidentWindowList: SameKeys<IncidentWindowListResponse, S['IncidentWindowListResponse']>;
    incidentWindowCreate: SameKeys<IncidentWindowCreate, S['IncidentWindowCreate']>;
    incidentWindowUpdate: SameKeys<IncidentWindowUpdate, S['IncidentWindowUpdate']>;
    incidentWindowWrite: SameKeys<IncidentWindowWriteResponse, S['IncidentWindowWriteResponse']>;
    incidentWindowDelete: SameKeys<IncidentWindowDeleteResponse, S['IncidentWindowDeleteResponse']>;
}

/** Every entry must be `true`. A drifted schema turns its entry into an
 *  object type and this assignment stops compiling. */
export const COMPOSITION_CONTRACT_PARITY: { [K in keyof CompositionContractParity]: CompositionContractParity[K] } = {
    composition: true,
    system: true,
    location: true,
    resource: true,
    mcpResource: true,
    policy: true,
    limit: true,
    dependency: true,
    observed: true,
    observedUndeclared: true,
    dependent: true,
    clientLabel: true,
    finding: true,
    edge: true,
    edgeList: true,
    draft: true,
    listComposition: true,
    members: true,
    tierSource: true,
    liveness: true,
    livenessStatus: true,
    livenessState: true,
    summary: true,
    health: true,
    dependencyHealth: true,
    boundary: true,
    fallbackCheck: true,
    dependencyCap: true,
    dependencyStatus: true,
    dependencyBasis: true,
    signalBasis: true,
    assumptions: true,
    evidenceProvided: true,
    separabilityDeclaration: true,
    separabilitySide: true,
    sealStatus: true,
    evidence: true,
    evidenceCreate: true,
    groupEnforce: true,
    groupCreateEnforce: true,
    groupUpdateEnforce: true,
    boardSystem: true,
    systemObligations: true,
    obligationTierSource: true,
    compositionAgent: true,
    agentReleaseRef: true,
    agentReleaseTrust: true,
    agentRelease: true,
    agentReleaseTrustTimeline: true,
    agentReleaseList: true,
    identityDeclaration: true,
    identityCreateDeclaration: true,
    identityUpdateDeclaration: true,
    runSummary: true,
    runDetail: true,
    runTrust: true,
    lifecycle: true,
    connection: true,
    connectionStatus: true,
    driftEvent: true,
    resolutionKind: true,
    baseline: true,
    baselineEpoch: true,
    epochKind: true,
    baselineEpochList: true,
    closeDriftRequest: true,
    humanResolutionKind: true,
    closeCauseCategory: true,
    driftResolution: true,
    incidentWindow: true,
    incidentCauseCategory: true,
    incidentResourceType: true,
    incidentWindowList: true,
    incidentWindowCreate: true,
    incidentWindowUpdate: true,
    incidentWindowWrite: true,
    incidentWindowDelete: true,
};
