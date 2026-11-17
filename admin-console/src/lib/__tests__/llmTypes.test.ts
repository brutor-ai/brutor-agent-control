/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The model-catalog helpers. Two carry real governance meaning rather than
 * cosmetics: a routing model is a virtual load-balancing group and not a
 * deployment, and Default-group membership is what makes a model reachable
 * by everyone — so "restricted" must never be inferred from an empty group
 * list.
 */

import { describe, it, expect } from 'vitest';
import {
    ADAPTER_DEFAULT_PORTS,
    ADAPTER_LABELS,
    COMPLEXITY_TIERS,
    DEFAULT_GROUP_ID,
    formatModelParameters,
    getAdapterLabel,
    getMachineStatusColor,
    getModelStatusColor,
    getRoutingGroupId,
    isGroupRestricted,
    isInDefaultGroup,
    isRoutingModel,
    type AdapterType,
    type DeployedModelStatus,
    type GroupInfo,
    type LLModelConfig,
    type MachineStatus,
} from '../llmTypes';

const model = (over: Partial<LLModelConfig> = {}): LLModelConfig =>
    ({ mode: 'chat', ...over }) as LLModelConfig;

const group = (id: string): GroupInfo => ({ id, name: id, is_active: true });

describe('getMachineStatusColor', () => {
    it('gives each machine status its own colour', () => {
        const seen = (['online', 'offline', 'unreachable', 'error'] as MachineStatus[]).map(
            getMachineStatusColor,
        );
        expect(new Set(seen).size).toBe(4);
        expect(getMachineStatusColor('online')).toContain('green');
        expect(getMachineStatusColor('error')).toContain('red');
    });

    it('falls back to the neutral style for an unknown status', () => {
        expect(getMachineStatusColor('retired' as MachineStatus)).toBe(
            getMachineStatusColor('offline'),
        );
    });
});

describe('getModelStatusColor', () => {
    it('groups the two in-flight states together', () => {
        expect(getModelStatusColor('pulling')).toBe(getModelStatusColor('starting'));
        expect(getModelStatusColor('pulling')).toContain('blue');
    });

    it('separates running, stopped, error and removing', () => {
        expect(getModelStatusColor('running')).toContain('green');
        expect(getModelStatusColor('error')).toContain('red');
        expect(getModelStatusColor('removing')).toContain('amber');
        expect(getModelStatusColor('stopped')).toContain('surface-raised');
    });

    it('falls back to neutral rather than throwing on an unknown status', () => {
        expect(getModelStatusColor('exploded' as DeployedModelStatus)).toBe(
            getModelStatusColor('stopped'),
        );
    });
});

describe('getAdapterLabel', () => {
    it('returns the human label for each known adapter', () => {
        expect(getAdapterLabel('dmr')).toBe(ADAPTER_LABELS.dmr);
        expect(getAdapterLabel('kserve')).toContain('KServe');
    });

    it('echoes an unknown adapter rather than rendering "undefined"', () => {
        expect(getAdapterLabel('vllm' as AdapterType)).toBe('vllm');
    });

    it('gives the Kubernetes adapters no port — they go through the K8s API', () => {
        expect(ADAPTER_DEFAULT_PORTS.dmr).toBeGreaterThan(0);
        expect(ADAPTER_DEFAULT_PORTS.kserve).toBe(0);
        expect(ADAPTER_DEFAULT_PORTS.llmd).toBe(0);
    });
});

describe('formatModelParameters', () => {
    it('renders sub-billion sizes in millions', () => {
        expect(formatModelParameters(0.5)).toBe('500M');
        expect(formatModelParameters(0.125)).toBe('125M');
    });

    it('keeps whole billions whole and gives fractions one decimal', () => {
        expect(formatModelParameters(8)).toBe('8B');
        expect(formatModelParameters(405)).toBe('405B');
        expect(formatModelParameters(1.3)).toBe('1.3B');
        expect(formatModelParameters(70.25)).toBe('70.3B');
    });

    it('distinguishes "unknown" from zero', () => {
        expect(formatModelParameters(null)).toBeNull();
        expect(formatModelParameters(undefined)).toBeNull();
        expect(formatModelParameters(0)).toBe('0M');
    });
});

describe('isRoutingModel / getRoutingGroupId', () => {
    it('recognises the virtual routing model by its mode', () => {
        expect(isRoutingModel(model({ mode: 'routing' }))).toBe(true);
        expect(isRoutingModel(model({ mode: 'chat' }))).toBe(false);
        expect(isRoutingModel(model({ mode: 'embedding' }))).toBe(false);
    });

    it('reads the routing group id out of model_name', () => {
        expect(getRoutingGroupId(model({ mode: 'routing', model_name: 'rg-7' }))).toBe('rg-7');
    });

    it('never reports a routing group for a real deployment', () => {
        // `model_name` on a real model is the upstream model id, not a group.
        expect(getRoutingGroupId(model({ mode: 'chat', model_name: 'gpt-5.2' }))).toBeNull();
    });

    it('is null when a routing model carries no group id', () => {
        expect(getRoutingGroupId(model({ mode: 'routing', model_name: '' }))).toBeNull();
        expect(getRoutingGroupId(model({ mode: 'routing' }))).toBeNull();
    });
});

describe('Default group membership', () => {
    it('is in the Default group when it names it', () => {
        expect(isInDefaultGroup(model({ groups: [group(DEFAULT_GROUP_ID)] }))).toBe(true);
        expect(isInDefaultGroup(model({ groups: [group('finance')] }))).toBe(false);
    });

    it('treats an absent or empty group list as not-in-Default', () => {
        expect(isInDefaultGroup(model())).toBe(false);
        expect(isInDefaultGroup(model({ groups: [] }))).toBe(false);
    });

    it('only calls a model restricted when it is bound somewhere other than Default', () => {
        expect(isGroupRestricted(model({ groups: [group('finance')] }))).toBe(true);
        expect(
            isGroupRestricted(model({ groups: [group('finance'), group(DEFAULT_GROUP_ID)] })),
        ).toBe(false);
    });

    it('does not call an unbound model restricted — that would invert the meaning', () => {
        expect(isGroupRestricted(model())).toBe(false);
        expect(isGroupRestricted(model({ groups: [] }))).toBe(false);
    });
});

describe('COMPLEXITY_TIERS', () => {
    it('runs cheapest to most capable — the routing engine walks this order', () => {
        expect(COMPLEXITY_TIERS).toEqual(['simple', 'medium', 'complex', 'reasoning']);
    });
});
