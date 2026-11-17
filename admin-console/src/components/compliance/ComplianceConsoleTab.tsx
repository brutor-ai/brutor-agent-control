/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Compliance console: one shell, nine pages, one
 * framework switcher.
 *
 * App.tsx routes every `compliance-*` id here; the catalogue in
 * `complianceSections.ts` maps the id to a page. The framework switcher is
 * fed by the registry, defaults to the EU AI Act, and remembers its
 * selection in localStorage.
 */

import { useCallback, useState } from 'react';
import { Scale } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { HelpTip } from '../ui/HelpTip';
import {
    COMPLIANCE_SECTIONS,
    DEFAULT_FRAMEWORK,
    readStoredFramework,
    resolveComplianceRoute,
    storeFramework,
    type CompliancePageProps,
    type ComplianceSection,
} from './complianceSections';
import OverviewPage from './pages/OverviewPage';
import FrameworksPage from './pages/FrameworksPage';
import ProfilesPage from './pages/ProfilesPage';
import ObligationsPage from './pages/ObligationsPage';
import EvidenceLedgerPage from './pages/EvidenceLedgerPage';
import OversightPage from './pages/OversightPage';
import TransparencyPage from './pages/TransparencyPage';
import ReportsPage from './pages/ReportsPage';
import IncidentsPage from './pages/IncidentsPage';

interface ComplianceConsoleTabProps {
    /** The current `activeTab`: a `compliance-*` id. */
    activeTab: string;
    /** Tab strip dispatch — receives a `compliance-*` route id. */
    onTabChange: (routeId: string) => void;
    /** App-wide navigation for links out of the console. */
    setActiveTab?: (tab: string) => void;
}

export default function ComplianceConsoleTab({ activeTab, onTabChange, setActiveTab }: ComplianceConsoleTabProps) {
    const { gatewayClient } = useGateway();
    const section = resolveComplianceRoute(activeTab);

    const registryLoader = useCallback(() => gatewayClient.listComplianceFrameworks(), [gatewayClient]);
    const {
        data: registry,
        error: frameworksError,
        reload: refreshFrameworks,
    } = useKeyedLoad('registry', registryLoader, 'Failed to load the framework registry');
    const allFrameworks = registry?.items ?? [];
    // Only frameworks checked against their published text are offered;
    // the rest are listed on the Frameworks page with the reason.
    const frameworks = allFrameworks.filter((f) => f.offered);

    // The switcher's own choice, remembered across visits.
    const [chosen, setChosen] = useState<string>(() => readStoredFramework() ?? DEFAULT_FRAMEWORK);
    // A remembered choice that is not offered falls back to the default.
    const framework =
        frameworks.length === 0 || frameworks.some((f) => f.id === chosen)
            ? chosen
            : (frameworks.find((f) => f.id === DEFAULT_FRAMEWORK) ?? frameworks[0]).id;
    const [focusObligation, setFocusObligation] = useState<string | null>(null);
    const setFramework = (id: string) => {
        setChosen(id);
        storeFramework(id);
        setFocusObligation(null);
    };

    // "Show this evidence under other frameworks": switch the framework and
    // land on that framework's obligation, opened.
    const openObligation = (frameworkId: string, obligationKey: string) => {
        setChosen(frameworkId);
        storeFramework(frameworkId);
        setFocusObligation(obligationKey);
        onTabChange('compliance-obligations');
    };

    const pageProps: CompliancePageProps = {
        framework,
        frameworks,
        registry: allFrameworks,
        refreshFrameworks,
        onTabChange,
        setActiveTab,
        focusObligation,
        openObligation,
    };

    return (
        <div className="app-column h-full" data-testid="compliance-console">
            <div className="px-6 pt-6 pb-3">
                <div className="flex items-start justify-between gap-4 flex-wrap">
                    <div className="flex items-start gap-2">
                        <Scale className="w-5 h-5 text-primary-600 mt-0.5" />
                        <div>
                            <div className="flex items-center gap-2">
                                <h1 className="text-xl font-semibold text-default">Compliance</h1>
                                <HelpTip text="Evidence first, frameworks second. Every governed action seals a record; a tenant log chains them and witnesses countersign its heads. A framework's obligations are then shown against those records — recomputed where the platform can, judged or declared where it cannot — with the basis on every statement." />
                            </div>
                            <p className="text-sm text-secondary">{section.description}</p>
                        </div>
                    </div>
                    <label className="flex items-center gap-2 text-sm">
                        <span className="text-xs text-muted uppercase tracking-wide">Framework</span>
                        <select
                            value={framework}
                            onChange={(e) => setFramework(e.target.value)}
                            className="rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                            data-testid="framework-switcher"
                            aria-label="Framework"
                        >
                            {frameworks.length === 0 && <option value={framework}>{framework}</option>}
                            {frameworks.map((f) => (
                                <option key={f.id} value={f.id}>
                                    {f.title}
                                    {f.enabled ? '' : ' (not enabled)'}
                                </option>
                            ))}
                        </select>
                    </label>
                </div>
                {frameworksError && (
                    <p className="mt-2 text-xs text-red-700" data-testid="frameworks-error">
                        {frameworksError}
                    </p>
                )}
            </div>

            <div className="flex border-b border-default bg-surface px-6 overflow-x-auto" role="tablist" aria-label="Compliance">
                {COMPLIANCE_SECTIONS.map(({ id, routeId, label, icon: Icon }) => {
                    const isActive = section.id === id;
                    return (
                        <button
                            key={id}
                            type="button"
                            role="tab"
                            aria-selected={isActive}
                            data-testid={`compliance-tab-${id}`}
                            onClick={() => onTabChange(routeId)}
                            className={`flex items-center gap-2 px-4 py-2.5 border-b-2 transition-colors text-sm font-medium whitespace-nowrap ${
                                isActive
                                    ? 'border-primary-500 text-primary-600'
                                    : 'border-transparent text-secondary hover:text-default hover:border-strong'
                            }`}
                        >
                            <Icon className="w-4 h-4" />
                            <span>{label}</span>
                        </button>
                    );
                })}
            </div>

            <div className="scroll-region p-6" data-testid="compliance-body">
                {renderPage(section.id, pageProps)}
            </div>
        </div>
    );
}

function renderPage(id: ComplianceSection, props: CompliancePageProps): React.ReactNode {
    switch (id) {
        case 'overview':
            return <OverviewPage {...props} />;
        case 'frameworks':
            return <FrameworksPage {...props} />;
        case 'profiles':
            return <ProfilesPage {...props} />;
        case 'obligations':
            return <ObligationsPage {...props} />;
        case 'evidence':
            return <EvidenceLedgerPage {...props} />;
        case 'oversight':
            return <OversightPage {...props} />;
        case 'transparency':
            return <TransparencyPage {...props} />;
        case 'reports':
            return <ReportsPage {...props} />;
        case 'incidents':
            return <IncidentsPage {...props} />;
        default:
            return null;
    }
}
