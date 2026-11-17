/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
    X,
    HelpCircle,
    Brain,
    Globe,
    Zap,
    Sliders,
    FileCode,
    Settings,
    Activity,
    CheckCircle2,
    AlertCircle,
    Info,
    Users,
    Lightbulb,
    Code,
    DollarSign,
    Shield,
    Network,
    Share2,
    MessageSquare,
    List,
    Layers
} from 'lucide-react';

interface LLModelFormHelpProps {
    isOpen: boolean;
    onClose: () => void;
    initialSection?: HelpSection;
}

type HelpSection = 'basic' | 'cost' | 'access' | 'media' | 'api' | 'generation' | 'tuning' | 'output' | 'tools' | 'params' | 'routing' | 'prompt' | 'retirement';

const LLModelFormHelp: React.FC<LLModelFormHelpProps> = ({
                                                             isOpen,
                                                             onClose,
                                                             initialSection = 'basic'
                                                         }) => {
    const [activeSection, setActiveSection] = React.useState<HelpSection>(initialSection);

    if (!isOpen) return null;

    const sections = [
        { id: 'basic' as HelpSection, label: 'General', icon: Brain },
        { id: 'cost' as HelpSection, label: 'Cost', icon: DollarSign },
        { id: 'api' as HelpSection, label: 'API Config', icon: Globe },
        { id: 'access' as HelpSection, label: 'Access Control', icon: Users },
        { id: 'generation' as HelpSection, label: 'Generation', icon: Zap },
        { id: 'tuning' as HelpSection, label: 'Tuning', icon: Sliders },
        { id: 'output' as HelpSection, label: 'Output', icon: FileCode },
        { id: 'tools' as HelpSection, label: 'Tools', icon: Settings },
        { id: 'params' as HelpSection, label: 'Parameter Control', icon: List },
        { id: 'routing' as HelpSection, label: 'Routing', icon: Activity },
        { id: 'prompt' as HelpSection, label: 'System Prompt', icon: MessageSquare }
    ];

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-6xl w-full max-h-[90vh] flex flex-col">
                <div className="flex items-center justify-between px-6 py-4 border-b border-default flex-shrink-0">
                    <div className="flex items-center space-x-2">
                        <HelpCircle className="w-5 h-5 text-primary-600" />
                        <h2 className="text-xl font-semibold text-default">
                            LLM Configuration Help
                        </h2>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <div className="flex flex-1 overflow-hidden min-h-0 min-w-0">
                    <div className="w-48 border-r border-default flex-shrink-0 overflow-y-auto">
                        <nav className="p-2 space-y-1">
                            {sections.map((section) => {
                                const Icon = section.icon;
                                return (
                                    <button
                                        key={section.id}
                                        onClick={() => setActiveSection(section.id)}
                                        className={`w-full flex items-center space-x-2 px-3 py-2 rounded-lg text-sm transition-colors ${
                                            activeSection === section.id
                                                ? 'bg-primary-50 text-primary-700 font-medium'
                                                : 'text-secondary hover:bg-surface-alt'
                                        }`}
                                    >
                                        <Icon className="w-4 h-4 flex-shrink-0" />
                                        <span className="truncate">{section.label}</span>
                                    </button>
                                );
                            })}
                        </nav>
                    </div>

                    <div className="scroll-region p-6">
                        {activeSection === 'basic' && <BasicInfoHelp />}
                        {activeSection === 'cost' && <CostHelp />}
                        {activeSection === 'access' && <AccessControlHelp />}
                        {activeSection === 'api' && <APIConfigHelp />}
                        {activeSection === 'generation' && <GenerationHelp />}
                        {activeSection === 'tuning' && <TuningHelp />}
                        {activeSection === 'output' && <OutputHelp />}
                        {activeSection === 'tools' && <ToolsHelp />}
                        {activeSection === 'params' && <ParamManagementHelp />}
                        {activeSection === 'routing' && <RoutingHelp />}
                        {activeSection === 'prompt' && <SystemPromptHelp />}
                    </div>
                </div>

                <div className="px-6 py-4 border-t border-default bg-surface-alt rounded-b-lg flex justify-between items-center flex-shrink-0">
                    <div className="flex items-center space-x-2 text-sm text-secondary">
                        <Info className="w-4 h-4" />
                        <span>Based on provider documentation</span>
                    </div>
                    <button
                        onClick={onClose}
                        className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

interface InfoBoxProps {
    icon: React.ReactNode;
    title: string;
    variant?: 'info' | 'success' | 'warning' | 'error';
    children: React.ReactNode;
}

const InfoBox: React.FC<InfoBoxProps> = ({ icon, title, children }) => {
    // All boxes use skyblue theme
    return (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
            <div className="flex items-start space-x-3">
                <div className="flex-shrink-0 text-blue-600">
                    {icon}
                </div>
                <div className="flex-1 min-w-0 min-h-0">
                    <h4 className="text-sm font-semibold text-blue-900 mb-2">{title}</h4>
                    {children}
                </div>
            </div>
        </div>
    );
};
const BasicInfoHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Basic Information</h3>
            <p className="text-sm text-secondary">
                Configure the essential model identification and capabilities.
            </p>
        </div>

        <InfoBox icon={<Brain className="w-5 h-5" />} title="Model Name" variant="info">
            <p className="text-sm mb-2">
                A friendly, descriptive name for this model deployment. This helps you identify
                the model in the UI and distinguish between multiple configurations.
            </p>
            <div className="bg-surface-alt rounded p-3 text-sm space-y-1">
                <div><strong>Good:</strong> GPT-4o Production, Claude Sonnet Dev, Gemini Flash Support</div>
                <div><strong>Bad:</strong> model1, test, my-model</div>
            </div>
        </InfoBox>

        <InfoBox icon={<Code className="w-5 h-5" />} title="Model Identifier" variant="info">
            <p className="text-sm mb-2">
                The exact model ID as recognized by the provider. Must match the provider's API exactly.
            </p>
            <div className="bg-surface border border-default rounded p-3 text-sm">
                <strong className="text-default">Common Model IDs:</strong>
                <div className="mt-2 space-y-1 font-mono text-xs">
                    <div>OpenAI: gpt-5.5, gpt-5.2, gpt-6-luna</div>
                    <div>Anthropic: claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5</div>
                    <div>Google: gemini-3.8-flash, gemini-3.5-flash-lite, gemini-4-argon</div>
                    <div>Groq: openai/gpt-oss-120b, qwen/qwen3.8-27b</div>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<CheckCircle2 className="w-5 h-5" />} title="Model Capabilities" variant="success">
            <p className="text-sm mb-3">
                Declare which features this model supports. Affects how the model can be used.
            </p>
            <div className="space-y-3">
                <div className="bg-surface border border-default rounded p-3">
                    <div className="font-medium text-sm mb-1">✓ Streaming</div>
                    <p className="text-xs text-secondary">
                        Model can return responses token-by-token in real-time. Most modern models support this.
                    </p>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <div className="font-medium text-sm text-default mb-1">✓ Function Calling</div>
                    <p className="text-xs text-secondary">
                        <strong>Required for MCP integration!</strong> Model can call tools/functions.
                        Supported by: GPT-4, Claude, Gemini 1.5+, Command R+
                    </p>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <div className="font-medium text-sm mb-1">✓ Vision</div>
                    <p className="text-xs text-secondary">
                        Model can analyze images. Examples: GPT-4o, Claude Sonnet 4, Gemini 1.5 Pro
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Sliders className="w-5 h-5" />} title="Parameters (Model Size)" variant="info">
            <p className="text-sm mb-3">
                Specify the model's size in billions of parameters. This is displayed as an
                informational badge throughout the UI to help administrators quickly compare models.
            </p>
            <div className="space-y-2 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Display Format:</strong>
                    <div className="mt-2 space-y-1 text-xs font-mono">
                        <div><code>0.5</code> → <span className="px-1.5 py-0.5 bg-indigo-50 text-indigo-700 rounded border border-indigo-200 font-semibold">500M</span></div>
                        <div><code>8</code> → <span className="px-1.5 py-0.5 bg-indigo-50 text-indigo-700 rounded border border-indigo-200 font-semibold">8B</span></div>
                        <div><code>70</code> → <span className="px-1.5 py-0.5 bg-indigo-50 text-indigo-700 rounded border border-indigo-200 font-semibold">70B</span></div>
                        <div><code>405</code> → <span className="px-1.5 py-0.5 bg-indigo-50 text-indigo-700 rounded border border-indigo-200 font-semibold">405B</span></div>
                    </div>
                </div>
                <div className="bg-surface-alt rounded p-3">
                    <strong>Note:</strong> This field is optional and display-only — it does not affect model
                    behaviour or routing. Leave empty if the parameter count is unknown or not applicable
                    (e.g. for proprietary APIs like GPT-4o where the exact size isn't published).
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Sliders className="w-5 h-5" />} title="Usage" variant="info">
            <p className="text-sm mb-2">
                Describe the intended use case for this model. This helps administrators
                and team members understand what each model is configured for.
            </p>
            <div className="bg-surface-alt rounded p-3 text-sm space-y-1">
                <div><strong>Examples:</strong></div>
                <div>• Customer support chatbot — handles tier-1 enquiries</div>
                <div>• Code review assistant for the engineering team</div>
                <div>• Document summarisation for legal department</div>
            </div>
        </InfoBox>

        <InfoBox icon={<Sliders className="w-5 h-5" />} title="Tags" variant="info">
            <p className="text-sm mb-2">
                Comma-separated tags for categorising and filtering models.
                Tags help organise models by cost tier, speed, capability, or team.
            </p>
            <div className="bg-surface-alt rounded p-3 text-sm space-y-1">
                <div><strong>Examples:</strong> coding, fast, multilingual, vision, cheap, premium, production, dev</div>
            </div>
        </InfoBox>
    </div>
);

const CostHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Token Cost Configuration</h3>
            <p className="text-sm text-secondary">
                Set per-token costs to enable usage tracking and cost center calculations in the dashboard.
            </p>
        </div>

        <InfoBox icon={<DollarSign className="w-5 h-5" />} title="Input Cost (per 1M tokens)">
            <p className="text-sm mb-2">
                The cost in USD charged by the provider for processing <strong>1 million input tokens</strong> (the prompt/context sent to the model).
            </p>
            <p className="text-sm">
                This is used to calculate costs in the usage dashboard and for cost center allocation.
            </p>
        </InfoBox>

        <InfoBox icon={<DollarSign className="w-5 h-5" />} title="Output Cost (per 1M tokens)">
            <p className="text-sm mb-2">
                The cost in USD charged by the provider for generating <strong>1 million output tokens</strong> (the model's response).
            </p>
            <p className="text-sm">
                Output tokens are typically more expensive than input tokens.
            </p>
        </InfoBox>

        <InfoBox icon={<Lightbulb className="w-5 h-5" />} title="Where to find pricing">
            <ul className="list-disc list-inside text-sm space-y-1">
                <li>OpenAI: openai.com/pricing</li>
                <li>Anthropic: anthropic.com/pricing</li>
                <li>Google: ai.google.dev/pricing</li>
                <li>HuggingFace: Check your HF billing dashboard</li>
                <li>Self-hosted (vLLM): Calculate from GPU cost / tokens served</li>
            </ul>
        </InfoBox>
    </div>
);

const AccessControlHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Access Control</h3>
            <p className="text-sm text-secondary">
                Control which Resource Groups can access this model. Groups enable per-group cost tracking,
                usage monitoring, alerts, and access policies.
            </p>
        </div>

        <InfoBox icon={<Users className="w-5 h-5" />} title="How Resource Group Access Works" variant="info">
            <div className="space-y-3 text-sm">
                <p>
                    Every model automatically belongs to the <strong>Default group</strong>, which gives all users
                    basic access. You can assign additional Resource Groups to enable fine-grained control.
                </p>
                <div className="bg-surface border border-default rounded p-3 space-y-2">
                    <div className="flex items-start space-x-2">
                        <CheckCircle2 className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
                        <div>
                            <strong className="text-default">Default Group (always assigned)</strong>
                            <p className="text-xs text-secondary mt-0.5">
                                Every model belongs to the Default group. This ensures all users have access unless
                                you configure more restrictive group-based policies.
                            </p>
                        </div>
                    </div>
                    <div className="flex items-start space-x-2">
                        <Layers className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
                        <div>
                            <strong className="text-default">Additional Groups (optional)</strong>
                            <p className="text-xs text-secondary mt-0.5">
                                Assign the model to specific groups (teams, projects, agents) to enable per-group
                                analytics, budget limits, and access scoping.
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<DollarSign className="w-5 h-5" />} title="What Groups Enable" variant="info">
            <div className="space-y-3 text-sm">
                <p>Assigning a model to a Resource Group unlocks these features for that group:</p>
                <div className="grid grid-cols-2 gap-3">
                    <div className="bg-surface border border-default rounded p-3">
                        <strong className="text-default text-xs block mb-1">Cost Tracking</strong>
                        <p className="text-xs text-secondary">
                            Track token spend and API costs per group. See which team or agent is consuming what.
                        </p>
                    </div>
                    <div className="bg-surface border border-default rounded p-3">
                        <strong className="text-default text-xs block mb-1">Usage Monitoring</strong>
                        <p className="text-xs text-secondary">
                            Daily/monthly request counts, token volumes, and response times per group.
                        </p>
                    </div>
                    <div className="bg-surface border border-default rounded p-3">
                        <strong className="text-default text-xs block mb-1">Budget Alerts</strong>
                        <p className="text-xs text-secondary">
                            Set warning thresholds and hard stops per group to prevent runaway costs.
                        </p>
                    </div>
                    <div className="bg-surface border border-default rounded p-3">
                        <strong className="text-default text-xs block mb-1">Access Policies</strong>
                        <p className="text-xs text-secondary">
                            Restrict model access to members of specific groups via API keys or user mapping.
                        </p>
                    </div>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Network className="w-5 h-5" />} title="Inheritance" variant="info">
            <div className="space-y-3 text-sm">
                <p>
                    Resource Groups form a hierarchy. When a model is assigned to a parent group, all child groups
                    automatically inherit access (unless inheritance is disabled on a child).
                </p>
                <div className="bg-surface border border-default rounded p-3">
                    <div className="text-xs font-mono space-y-1">
                        <div>📁 Engineering <span className="text-green-600">(model assigned here)</span></div>
                        <div className="ml-4">📁 Frontend Team <span className="text-muted">→ inherits access</span></div>
                        <div className="ml-8">📁 Customer Portal <span className="text-muted">→ inherits access</span></div>
                        <div className="ml-4">📁 Backend Team <span className="text-muted">→ inherits access</span></div>
                    </div>
                </div>
                <p className="text-xs text-secondary">
                    Assign the model at the highest relevant level. Child groups get access automatically.
                    Usage limits, however, are not inherited — each group defines its own budget and rate limits.
                </p>
            </div>
        </InfoBox>

        <InfoBox icon={<Lightbulb className="w-5 h-5" />} title="Best Practices" variant="success">
            <div className="space-y-3 text-sm">
                <ul className="list-disc list-inside space-y-2 ml-2">
                    <li>
                        <strong>Start broad, narrow later:</strong> Keep models in the Default group initially,
                        then assign to specific groups as your organisation structure takes shape.
                    </li>
                    <li>
                        <strong>One group per use case:</strong> Create groups for each distinct application,
                        agent, or team so you can track costs and usage independently.
                    </li>
                    <li>
                        <strong>Use hierarchy for inheritance:</strong> Assign models at the department or team
                        level rather than individually to each project group.
                    </li>
                    <li>
                        <strong>Set budget limits per group:</strong> After assigning groups, configure LLM Limits
                        in the group settings to prevent unexpected costs.
                    </li>
                </ul>
            </div>
        </InfoBox>

        <InfoBox icon={<Shield className="w-5 h-5" />} title="Access Resolution" variant="info">
            <div className="space-y-3 text-sm">
                <p>When a request comes in, the gateway determines access as follows:</p>
                <div className="bg-surface border border-default rounded p-3">
                    <ol className="list-decimal list-inside space-y-1.5 text-xs">
                        <li>Identify the user's group (via API key or user-to-group mapping)</li>
                        <li>Collect the group's effective models (direct + inherited from ancestors)</li>
                        <li>Check if the requested model is in the effective set</li>
                        <li>Apply the group's usage limits (budget, tokens, request rate)</li>
                        <li>Route the request if all checks pass</li>
                    </ol>
                </div>
                <p className="text-xs text-secondary">
                    If the model is in the Default group, all users have access regardless of their specific
                    group membership.
                </p>
            </div>
        </InfoBox>
    </div>
);

const APIConfigHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">API Configuration</h3>
            <p className="text-sm text-secondary">
                Configure how to connect to the model's API endpoint.
            </p>
        </div>

        <InfoBox icon={<Lightbulb className="w-5 h-5" />} title="Auto-Discover API URL" variant="success">
            <p className="text-sm mb-3">
                <strong>Enable this for most providers!</strong> The gateway automatically knows the correct API endpoint for standard providers.
            </p>
            <div className="bg-surface border border-default rounded p-4">
                <strong className="text-default text-sm">✓ Works Automatically:</strong>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                    <div>• OpenAI</div>
                    <div>• Anthropic</div>
                    <div>• Google Gemini</div>
                    <div>• Cohere</div>
                    <div>• Mistral</div>
                    <div>• Groq</div>
                    <div>• Perplexity</div>
                    <div>• Together AI</div>
                </div>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-4">
                <strong className="text-default text-sm">⚙️ Requires Custom URL:</strong>
                <div className="mt-2 space-y-2 text-xs">
                    <div><strong>Azure OpenAI:</strong> Your deployment URL required</div>
                    <div><strong>Ollama:</strong> Local server URL (e.g., http://localhost:11434)</div>
                    <div><strong>Self-hosted:</strong> Your custom API endpoint</div>
                </div>
                <p className="mt-2 text-xs">
                    Which models need one is the Control Plane&apos;s to say. The form asks for the URL when the model catalog marks a model of that provider and mode as needing it; otherwise a save without one is refused, and the reason is shown next to the API URL field.
                </p>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-4">
                <strong className="text-default text-sm">Amazon Bedrock:</strong>
                <p className="mt-2 text-xs">
                    No URL is needed: the gateway uses the Bedrock address of the model&apos;s AWS region. Enter the model&apos;s own AWS access key ID, secret access key and region; the gateway signs every request with them and refuses the model (501) without them. The credential is stored encrypted and never shown again; leave the fields empty to keep it.
                </p>
            </div>
        </InfoBox>

        <InfoBox icon={<Globe className="w-5 h-5" />} title="Custom API URL" variant="info">
            <p className="text-sm mb-3">
                Only needed when auto-discovery is disabled. Must be the complete API endpoint.
            </p>
            <div className="space-y-2 text-sm font-mono text-xs">
                <div className="bg-surface-alt rounded p-3">
                    <div className="font-sans font-medium mb-1">AWS Bedrock:</div>
                    <code>https://bedrock-runtime.us-east-1.amazonaws.com</code>
                </div>
                <div className="bg-surface-alt rounded p-3">
                    <div className="font-sans font-medium mb-1">Azure OpenAI:</div>
                    <code>https://your-resource.openai.azure.com/...</code>
                </div>
                <div className="bg-surface-alt rounded p-3">
                    <div className="font-sans font-medium mb-1">Ollama (Local):</div>
                    <code>http://localhost:11434/api/chat</code>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Settings className="w-5 h-5" />} title="Authentication" variant="info">
            <div className="space-y-3 text-sm">
                <div className="bg-blue-50 border border-blue-200 rounded p-3">
                    <strong className="text-blue-900">API Key (Most Common):</strong>
                    <p className="text-xs text-blue-800 mt-1">
                        Standard API key authentication. Keep your keys secure and never commit to version control.
                    </p>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong>OAuth:</strong>
                    <p className="text-xs text-secondary mt-1">
                        Enterprise authentication using OAuth 2.0. Requires IdP server configuration.
                    </p>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong>None:</strong>
                    <p className="text-xs text-secondary mt-1">
                        For local models or endpoints that don't require authentication (e.g., local Ollama).
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Settings className="w-5 h-5" />} title="Extra Headers" variant="info">
            <p className="text-sm mb-2">
                Additional HTTP headers sent with every request to this model's API.
                Useful for custom deployments, proxy authentication, or provider-specific requirements.
            </p>
            <div className="bg-surface-alt rounded p-3 text-xs font-mono">
                <div className="font-sans font-medium mb-1 text-sm">Example:</div>
                <pre>{`{
  "X-Organization-Id": "org-123",
  "X-Custom-Auth": "bearer-token"
}`}</pre>
            </div>
        </InfoBox>
    </div>
);
const GenerationHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Generation Settings</h3>
            <p className="text-sm text-secondary">
                Control how the model generates text responses.
            </p>
        </div>

        <InfoBox icon={<Zap className="w-5 h-5" />} title="Temperature" variant="info">
            <p className="text-sm mb-3">
                Controls the randomness of the model's output by scaling the probability distribution. <strong>Range: 0.0 - 2.0, Default: 1.0</strong>
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">How it works:</strong>
                <p className="text-xs text-blue-800 mt-1">
                    Lower values make the model more focused and deterministic by reducing randomness in token selection.
                    Higher values increase creativity and variation by allowing more diverse token choices.
                </p>
            </div>
            <div className="space-y-2">
                <div className="bg-surface-alt border border-default rounded p-3 text-sm">
                    <strong className="text-default">Very Low (0.0 - 0.2):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Nearly deterministic, always chooses most likely tokens. Perfect for tasks requiring consistency.
                    </p>
                    <div className="mt-2 text-xs"><strong>Best for:</strong> Code generation, data extraction, classification, factual Q&A</div>
                    <div className="mt-1 font-mono text-xs text-secondary">Example: 0.0 for deterministic output, 0.2 for code</div>
                </div>
                <div className="bg-surface border border-default rounded p-3 text-sm">
                    <strong className="text-default">Balanced (0.5 - 0.8):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Good balance between creativity and consistency. Most versatile range.
                    </p>
                    <div className="mt-2 text-xs"><strong>Best for:</strong> General chat, customer support, summarization, explanations</div>
                    <div className="mt-1 font-mono text-xs text-secondary">Example: 0.7 for conversational AI</div>
                </div>
                <div className="bg-surface border border-default rounded p-3 text-sm">
                    <strong className="text-default">High (0.9 - 2.0):</strong>
                    <p className="text-xs text-secondary mt-1">
                        More random and creative, explores diverse possibilities. Can produce unexpected results.
                    </p>
                    <div className="mt-2 text-xs"><strong>Best for:</strong> Creative writing, brainstorming, poetry, unconventional ideas</div>
                    <div className="mt-1 font-mono text-xs text-secondary">Example: 1.0-1.2 for creative content</div>
                </div>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong>⚠️ Important:</strong> OpenAI recommends altering temperature OR top_p, but not both.
                Adjusting both can lead to unpredictable behavior.
            </div>
        </InfoBox>

        <InfoBox icon={<FileCode className="w-5 h-5" />} title="Max Tokens" variant="info">
            <p className="text-sm mb-3">
                Maximum number of tokens the model can generate in the response. Also called <code>max_completion_tokens</code> in some APIs.
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">What are tokens?</strong>
                <ul className="mt-2 space-y-1 text-xs text-blue-800 list-disc ml-4">
                    <li>Tokens are pieces of words used by the model (not full words)</li>
                    <li>1 token ≈ 4 characters in English (roughly 3/4 of a word)</li>
                    <li>100 tokens ≈ 75 words</li>
                    <li>Both input (prompt) and output (completion) count toward limits</li>
                </ul>
            </div>
            <div className="bg-surface border border-default rounded p-3 text-sm mb-3">
                <strong className="text-default">Cost & Performance Impact:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li>Setting too low may truncate responses mid-sentence</li>
                    <li>Setting too high increases costs (you pay per token)</li>
                    <li>Higher values also increase latency (takes longer to generate)</li>
                    <li>Set based on expected response length, not model's maximum</li>
                </ul>
            </div>
            <div className="space-y-2 text-sm">
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong>Short responses (64-512 tokens):</strong>
                    <div className="text-xs text-secondary mt-1">Quick answers, single paragraphs, yes/no with explanation</div>
                    <div className="text-xs mt-1"><strong>Use cases:</strong> Classification, quick Q&A, sentiment analysis</div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong>Medium responses (512-2048 tokens):</strong>
                    <div className="text-xs text-secondary mt-1">Multiple paragraphs, detailed explanations, structured data</div>
                    <div className="text-xs mt-1"><strong>Use cases:</strong> Email drafts, blog summaries, technical explanations</div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong>Long responses (2048-8192 tokens):</strong>
                    <div className="text-xs text-secondary mt-1">Articles, detailed analysis, comprehensive documents</div>
                    <div className="text-xs mt-1"><strong>Use cases:</strong> Content generation, report writing, code with documentation</div>
                </div>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong className="text-default">Pro Tip:</strong> Start conservative (512-1024) and increase only if responses
                are being cut off. You can always increase later, but higher limits cost more.
            </div>
        </InfoBox>

        <InfoBox icon={<Sliders className="w-5 h-5" />} title="Top P (Nucleus Sampling)" variant="info">
            <p className="text-sm mb-3">
                Alternative to temperature for controlling randomness. Limits token selection to those whose cumulative probability reaches the threshold. <strong>Range: 0.0 - 1.0, Default: 1.0</strong>
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">How it works:</strong>
                <p className="text-xs text-blue-800 mt-2">
                    The model considers only the smallest set of tokens whose cumulative probability mass adds up to <code>top_p</code>.
                    For example, with <code>top_p=0.1</code>, only tokens comprising the top 10% probability mass are considered.
                </p>
                <p className="text-xs text-blue-800 mt-2">
                    This method dynamically adjusts the number of tokens considered based on their probability distribution,
                    unlike temperature which affects all tokens equally.
                </p>
            </div>
            <div className="space-y-2 text-sm">
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">0.1 (Very Focused):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Only the tokens comprising the top 10% probability mass. Extremely deterministic.
                    </p>
                    <div className="mt-1 text-xs"><strong>Use for:</strong> High-precision tasks, factual responses</div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">0.5 (Balanced):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Considers tokens in the top 50% probability mass. Good balance of quality and diversity.
                    </p>
                    <div className="mt-1 text-xs"><strong>Use for:</strong> Most general-purpose applications</div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">0.9 (Diverse):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Considers tokens comprising up to 90% probability mass. More creative outputs.
                    </p>
                    <div className="mt-1 text-xs"><strong>Use for:</strong> Creative tasks, brainstorming</div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">1.0 (No Limit):</strong>
                    <p className="text-xs text-secondary mt-1">
                        All tokens considered (no filtering). Equivalent to not using nucleus sampling.
                    </p>
                </div>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong>⚠️ Important:</strong> OpenAI recommends adjusting either temperature or top_p, not both.
                For most use cases, adjusting temperature is more intuitive.
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong className="text-default">Pro Tip:</strong> For reproducible outputs, set both <code>temperature=0</code>
                and <code>top_p=1</code> (or use seed parameter). For creative tasks, try <code>temperature=0.7</code>
                or <code>top_p=0.9</code>.
            </div>
        </InfoBox>

        <InfoBox icon={<CheckCircle2 className="w-5 h-5" />} title="Seed (Reproducibility)" variant="info">
            <p className="text-sm mb-3">
                Integer seed for deterministic sampling. Makes the system attempt to return reproducible outputs with the same seed and parameters.
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">How it works:</strong>
                <p className="text-xs text-blue-800 mt-2">
                    When provided, the system makes a best effort to sample deterministically, so repeated requests with the
                    same seed and parameters should return similar results. Determinism is not guaranteed.
                </p>
                <p className="text-xs text-blue-800 mt-2">
                    Monitor the <code>system_fingerprint</code> response field to detect backend changes that might affect determinism.
                </p>
            </div>
            <div className="bg-surface border border-default rounded p-3 text-sm mb-3">
                <strong className="text-default">When to use seeds:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li><strong>Testing:</strong> Get consistent outputs to verify behavior</li>
                    <li><strong>Debugging:</strong> Reproduce issues reliably for investigation</li>
                    <li><strong>A/B Testing:</strong> Compare prompt variations with same randomness</li>
                    <li><strong>Regression Testing:</strong> Ensure outputs don't change unexpectedly</li>
                    <li><strong>Demos:</strong> Show consistent examples in presentations</li>
                </ul>
            </div>
            <div className="bg-surface-alt border border-default rounded p-3 text-sm">
                <strong>Best Practices:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li>Use any integer value (commonly timestamp or random number)</li>
                    <li>Same seed + same prompt + same parameters = same-ish output</li>
                    <li>Works best with <code>temperature=1</code> or lower</li>
                    <li>Don't rely on perfect reproducibility for critical applications</li>
                </ul>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong>⚠️ Important:</strong> Not all providers support seeds. OpenAI, Anthropic Claude, and some others do.
                Check your provider's documentation. Backend updates may affect reproducibility even with seeds.
            </div>
        </InfoBox>
    </div>
);

const TuningHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Advanced Tuning</h3>
            <p className="text-sm text-secondary">
                Fine-tune model behavior with penalties and token biases.
            </p>
        </div>

        <div className="bg-surface border border-default rounded-lg p-4">
            <div className="flex items-start space-x-2">
                <AlertCircle className="w-5 h-5 text-secondary flex-shrink-0 mt-0.5" />
                <div className="text-sm text-default">
                    <strong>Advanced Feature:</strong> Most use cases work well without these settings.
                    Only adjust if you understand the implications.
                </div>
            </div>
        </div>

        <InfoBox icon={<Sliders className="w-5 h-5" />} title="Presence Penalty" variant="info">
            <p className="text-sm mb-3">
                Penalizes tokens based on whether they have appeared in the text so far, regardless of frequency. <strong>Range: -2.0 to 2.0, Default: 0.0</strong>
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">How it works:</strong>
                <p className="text-xs text-blue-800 mt-2">
                    Positive values penalize ANY token that has already appeared, encouraging the model to talk about new topics and introduce fresh concepts.
                    The penalty is applied once per token, regardless of how many times it appeared.
                </p>
                <p className="text-xs text-blue-800 mt-2">
                    Think of it as: "Have we mentioned this topic/word before? If yes, let's try something else."
                </p>
            </div>
            <div className="space-y-2 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Positive Values (0.1 - 2.0):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Increases the model's likelihood to explore new topics. Discourages repeating any concept, even if appropriate.
                    </p>
                    <div className="mt-2 text-xs">
                        <strong>Use for:</strong> Creative writing, brainstorming diverse ideas, exploring multiple aspects of a topic
                    </div>
                    <div className="mt-1 text-xs"><strong>Try:</strong> 0.5-1.0 for creative content generation</div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">Zero (0.0, default):</strong>
                    <p className="text-xs text-secondary mt-1">
                        No penalty. Model freely repeats topics when contextually appropriate.
                    </p>
                    <div className="mt-1 text-xs"><strong>Use for:</strong> Most general-purpose applications</div>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Negative Values (-2.0 to -0.1):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Encourages the model to reuse topics (rarely useful). Generally not recommended.
                    </p>
                </div>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong className="text-default">Presence vs Frequency:</strong> Presence penalty treats all repeated tokens equally
                (appeared once = appeared 100 times). Use this to encourage topical diversity.
            </div>
        </InfoBox>

        <InfoBox icon={<Sliders className="w-5 h-5" />} title="Frequency Penalty" variant="info">
            <p className="text-sm mb-3">
                Penalizes tokens based on their existing frequency in the text so far, proportional to how often they've appeared. <strong>Range: -2.0 to 2.0, Default: 0.0</strong>
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">How it works:</strong>
                <p className="text-xs text-blue-800 mt-2">
                    Positive values penalize tokens based on how frequently they've appeared. The more times a token appears,
                    the stronger the penalty. This decreases the model's likelihood to repeat the same line verbatim.
                </p>
                <p className="text-xs text-blue-800 mt-2">
                    Think of it as: "How many times have we used this word? The more we've used it, the less likely we should use it again."
                </p>
            </div>
            <div className="space-y-2 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Positive Values (0.1 - 2.0):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Reduces likelihood of repeating the same words/phrases. The penalty increases with each repetition.
                    </p>
                    <div className="mt-2 text-xs">
                        <strong>Use for:</strong> Reducing repetitive text, avoiding "verbal tics," varying vocabulary
                    </div>
                    <div className="mt-2 space-y-1 text-xs">
                        <div><code className="bg-surface px-1 rounded">0.2-0.3</code> - Gentle reduction (good starting point)</div>
                        <div><code className="bg-surface px-1 rounded">0.5-1.0</code> - Moderate reduction</div>
                        <div><code className="bg-surface px-1 rounded">1.0-2.0</code> - Strong reduction (may affect quality)</div>
                    </div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">Zero (0.0, default):</strong>
                    <p className="text-xs text-secondary mt-1">
                        No penalty. Model repeats words naturally as context requires.
                    </p>
                    <div className="mt-1 text-xs"><strong>Use for:</strong> Most tasks where natural repetition is fine</div>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Negative Values (-2.0 to -0.1):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Encourages repetition (rarely useful). Generally not recommended.
                    </p>
                </div>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong className="text-default">Frequency vs Presence:</strong> Frequency penalty increases proportionally
                with repetitions (word used 10x gets stronger penalty than word used 2x). Use this to reduce exact repetition
                of specific words and phrases.
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong>⚠️ Warning:</strong> High values (&gt;1.0) can make output less coherent as the model avoids necessary
                repetition of key terms. Start low and increase gradually.
            </div>
        </InfoBox>

        <InfoBox icon={<Code className="w-5 h-5" />} title="Logit Bias" variant="warning">
            <p className="text-sm mb-3">
                Directly modify the likelihood of specific tokens appearing in the completion. <strong>Range: -100 to 100 per token</strong>
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm mb-3">
                <strong className="text-blue-900">How it works:</strong>
                <p className="text-xs text-blue-800 mt-2">
                    Accepts a JSON object mapping token IDs to bias values from -100 to 100. The bias is added to the
                    token's logits (pre-softmax scores) before sampling.
                </p>
                <ul className="mt-2 space-y-1 text-xs text-blue-800 list-disc ml-4">
                    <li>Values between -1 and 1: Subtle decrease or increase in likelihood</li>
                    <li>Values like -100: Effectively ban the token</li>
                    <li>Values like 100: Strongly favor the token (not exclusive selection)</li>
                </ul>
            </div>
            <div className="bg-surface border border-default rounded p-4 text-sm mb-3">
                <strong className="text-default">⚠️ Expert Feature:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li>Requires knowing exact token IDs from the model's tokenizer</li>
                    <li>Token IDs vary by model and tokenizer version</li>
                    <li>Use OpenAI's tokenizer tool to convert text to token IDs</li>
                    <li>Most users should use temperature, top_p, and penalties instead</li>
                </ul>
            </div>
            <div className="bg-surface-alt border border-default rounded p-3 text-sm">
                <strong className="text-default">Example Use Cases:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li>Forcing specific formatting (e.g., ban newline tokens for single-line output)</li>
                    <li>Biasing towards/against specific words in classification tasks</li>
                    <li>Fine-grained control over output vocabulary</li>
                </ul>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong>⚠️ Note:</strong> The exact effect varies per model. Start with small values (±1 to ±5) and test thoroughly.
            </div>
        </InfoBox>
    </div>
);
const OutputHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Response & Output</h3>
            <p className="text-sm text-secondary">
                Control output format and logging options.
            </p>
        </div>

        <InfoBox icon={<FileCode className="w-5 h-5" />} title="Response Format (JSON Mode)" variant="success">
            <p className="text-sm mb-3">
                Force the model to respond with valid JSON. Perfect for structured data extraction.
            </p>
            <div className="bg-surface border border-default rounded p-3">
                <strong className="text-default text-sm">Enable JSON Mode:</strong>
                <pre className="mt-2 text-xs font-mono overflow-x-auto">
{`{
  "type": "json_object"
}`}
                </pre>
                <p className="text-xs text-secondary mt-2">
                    Model will always return valid JSON. You must also prompt the model to return JSON.
                </p>
            </div>
            <div className="mt-3 bg-blue-50 border border-blue-200 rounded p-3">
                <strong className="text-blue-900 text-sm">Use Cases:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li>Data extraction from text</li>
                    <li>Structured form filling</li>
                    <li>API integrations requiring JSON</li>
                    <li>Parsing outputs programmatically</li>
                </ul>
            </div>
        </InfoBox>

        <InfoBox icon={<Activity className="w-5 h-5" />} title="Log Probabilities" variant="info">
            <p className="text-sm mb-3">
                Return the model's confidence (log probability) for generated tokens.
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-3 text-sm">
                <strong className="text-blue-900">What You Get:</strong>
                <p className="text-xs text-blue-800 mt-2">
                    For each token, see how confident the model was in choosing it.
                    Useful for debugging and understanding model behavior.
                </p>
            </div>
            <div className="mt-3 bg-surface border border-default rounded p-3 text-xs">
                <strong>Use Cases:</strong>
                <ul className="mt-2 space-y-1 ml-4 list-disc">
                    <li>Detect low-confidence hallucinations</li>
                    <li>Debug unexpected model outputs</li>
                    <li>Build confidence scores for responses</li>
                </ul>
            </div>
        </InfoBox>

        <InfoBox icon={<Zap className="w-5 h-5" />} title="Stream Options" variant="info">
            <p className="text-sm mb-3">
                Configure streaming behavior for real-time token-by-token responses.
            </p>
            <div className="bg-surface-alt border border-default rounded p-3">
                <strong className="text-sm">Include Usage Stats:</strong>
                <pre className="mt-2 text-xs font-mono">
{`{
  "include_usage": true
}`}
                </pre>
                <p className="text-xs text-secondary mt-2">
                    Get token usage information even in streaming mode. Useful for cost tracking.
                </p>
            </div>
        </InfoBox>
    </div>
);

const ToolsHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Tools & Function Calling</h3>
            <p className="text-sm text-secondary">
                Configure how the model interacts with external tools and functions.
            </p>
        </div>

        <div className="bg-surface border border-default rounded-lg p-4">
            <div className="flex items-start space-x-2">
                <AlertCircle className="w-5 h-5 text-secondary flex-shrink-0 mt-0.5" />
                <div className="text-sm text-default">
                    <strong>Required for MCP:</strong> Function calling must be enabled in Basic Info
                    tab to use Model Context Protocol (MCP) tool integration.
                </div>
            </div>
        </div>

        <InfoBox icon={<CheckCircle2 className="w-5 h-5" />} title="Parallel Tool Calls" variant="success">
            <p className="text-sm mb-3">
                Allow the model to call multiple functions simultaneously in one response.
            </p>
            <div className="bg-surface border border-default rounded p-3 text-sm">
                <strong className="text-default">Benefits:</strong>
                <ul className="mt-2 space-y-1 text-xs list-disc ml-4">
                    <li><strong>Faster:</strong> Complete multiple independent tasks at once</li>
                    <li><strong>Efficient:</strong> Single API call instead of sequential calls</li>
                    <li><strong>Better UX:</strong> Users get results faster</li>
                </ul>
            </div>
            <div className="mt-3 bg-blue-50 border border-blue-200 rounded p-3 text-xs">
                <strong>Example:</strong>
                <p className="mt-1">
                    User asks: "What's the weather in NYC and stock price of AAPL?"
                </p>
                <p className="mt-1">
                    Model calls <code>get_weather(NYC)</code> and <code>get_stock_price(AAPL)</code> simultaneously.
                </p>
            </div>
        </InfoBox>

        <InfoBox icon={<Settings className="w-5 h-5" />} title="Tool Choice Strategy" variant="info">
            <p className="text-sm mb-3">
                Control when the model should use available tools.
            </p>
            <div className="space-y-2 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Auto (Recommended):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Model decides when to use tools based on the user's query. Most flexible and natural.
                    </p>
                </div>
                <div className="bg-blue-50 border border-blue-200 rounded p-3">
                    <strong className="text-blue-900">None:</strong>
                    <p className="text-xs text-blue-800 mt-1">
                        Model never uses tools. Useful for pure text generation.
                    </p>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Required:</strong>
                    <p className="text-xs text-secondary mt-1">
                        Model MUST use a tool. Useful for workflows that always need tool interaction.
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Code className="w-5 h-5" />} title="Predefined Tools" variant="info">
            <p className="text-sm mb-3">
                Define tools/functions that should always be available to this model.
            </p>
            <div className="bg-surface-alt border border-default rounded p-3">
                <strong className="text-sm">Function Definition Format:</strong>
                <pre className="mt-2 text-xs font-mono overflow-x-auto">
{`[
  {
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get current weather",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "City and state"
          }
        },
        "required": ["location"]
      }
    }
  }
]`}
                </pre>
            </div>
        </InfoBox>
    </div>
);

const ParamManagementHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Parameter Management</h3>
            <p className="text-sm text-secondary">
                Control which OpenAI-compatible parameters are passed to the LLM. Some models don't support all parameters.
            </p>
        </div>

        <InfoBox title="Two Lists" icon={<Lightbulb className="w-5 h-5" />}>
            <div className="space-y-3">
                <div>
                    <p className="font-medium text-default mb-1">1. This model&apos;s own dropped parameters</p>
                    <p className="text-sm text-secondary">
                        The parameters listed on the model. The gateway removes them from every request to this
                        model. This list always applies.
                    </p>
                    <div className="mt-2 text-xs text-secondary bg-surface-alt p-2 rounded">
                        <strong>Example:</strong> temperature, top_p, presence_penalty
                    </div>
                </div>

                <div>
                    <p className="font-medium text-default mb-1">2. The provider&apos;s dropped-parameters list</p>
                    <p className="text-sm text-secondary">
                        The parameters listed on the model&apos;s provider. The gateway removes them from requests to
                        this model while <strong>Apply the provider&apos;s dropped-parameters list</strong> is on.
                        Switch it off for a model that accepts a parameter the provider&apos;s other models reject.
                    </p>
                    <div className="mt-2 text-xs text-secondary bg-surface-alt p-2 rounded">
                        <strong>Default:</strong> on. A parameter on neither list is sent to the provider as it came.
                    </div>
                </div>
            </div>
        </InfoBox>

        <InfoBox title="Common Parameter Drop Lists" icon={<Code className="w-5 h-5" />}>
            <div className="space-y-4 text-sm">
                <div>
                    <p className="font-medium text-default mb-1">OpenAI O1/O3 Reasoning Models</p>
                    <p className="text-xs text-secondary mb-2">
                        These models use fixed generation parameters and don't support customization
                    </p>
                    <code className="block text-xs bg-gray-900 text-gray-100 p-2 rounded">
                        temperature, top_p, presence_penalty, frequency_penalty, logit_bias,
                        logprobs, top_logprobs, n, parallel_tool_calls
                    </code>
                </div>

                <div>
                    <p className="font-medium text-default mb-1">OpenAI GPT-5 Models</p>
                    <p className="text-xs text-secondary mb-2">
                        Limited parameter support in GPT-5 family
                    </p>
                    <code className="block text-xs bg-gray-900 text-gray-100 p-2 rounded">
                        top_p, presence_penalty, frequency_penalty
                    </code>
                </div>

                <div>
                    <p className="font-medium text-default mb-1">Ollama Local Models</p>
                    <p className="text-xs text-secondary mb-2">
                        Local models have limited API compatibility
                    </p>
                    <code className="block text-xs bg-gray-900 text-gray-100 p-2 rounded">
                        logprobs, top_logprobs, logit_bias, response_format,
                        parallel_tool_calls, presence_penalty, frequency_penalty
                    </code>
                </div>

                <div>
                    <p className="font-medium text-default mb-1">AWS Bedrock Models</p>
                    <p className="text-xs text-secondary mb-2">
                        Bedrock has its own parameter set
                    </p>
                    <code className="block text-xs bg-gray-900 text-gray-100 p-2 rounded">
                        n, logprobs, top_logprobs, logit_bias, presence_penalty, frequency_penalty
                    </code>
                </div>

                <div>
                    <p className="font-medium text-default mb-1">Google Gemini Models</p>
                    <p className="text-xs text-secondary mb-2">
                        Gemini uses different parameter names
                    </p>
                    <code className="block text-xs bg-gray-900 text-gray-100 p-2 rounded">
                        logprobs, top_logprobs, logit_bias, presence_penalty,
                        frequency_penalty, parallel_tool_calls
                    </code>
                </div>
            </div>
        </InfoBox>

        <InfoBox title="Advanced: Nested Field Removal" icon={<Code className="w-5 h-5" />}>
            <div className="space-y-3 text-sm">
                <p className="text-secondary">
                    <strong>Remove nested fields</strong> within complex objects using JSONPath-like notation.
                    This is useful for provider-specific fields that don't translate well.
                </p>

                <div className="space-y-4">
                    <div>
                        <p className="font-medium text-default mb-1">Supported Syntax:</p>
                        <ul className="space-y-1 text-xs text-secondary ml-4 list-disc">
                            <li><code className="bg-surface-raised px-1 rounded">field</code> - Top-level field</li>
                            <li><code className="bg-surface-raised px-1 rounded">parent.child</code> - Nested object field</li>
                            <li><code className="bg-surface-raised px-1 rounded">array[*]</code> - All array elements</li>
                            <li><code className="bg-surface-raised px-1 rounded">array[0]</code> - Specific array index</li>
                            <li><code className="bg-surface-raised px-1 rounded">tools[*].input_examples</code> - Field in all array elements</li>
                            <li><code className="bg-surface-raised px-1 rounded">tools[0].metadata.field</code> - Specific index + nested field</li>
                        </ul>
                    </div>

                    <div>
                        <p className="font-medium text-default mb-1">Real-World Example: Claude Code + AWS Bedrock</p>
                        <p className="text-xs text-secondary mb-2">
                            AWS Bedrock Claude models don't support the <code className="bg-surface-raised px-1 rounded">input_examples</code> field
                            in tool definitions. Use nested field removal to strip it out:
                        </p>
                        <code className="block text-xs bg-gray-900 text-gray-100 p-2 rounded">
                            tools[*].input_examples
                        </code>
                        <p className="text-xs text-secondary mt-2">
                            This removes <code className="bg-surface-raised px-1 rounded">input_examples</code> from ALL tools in the array,
                            while keeping other tool fields intact.
                        </p>
                    </div>

                    <div>
                        <p className="font-medium text-default mb-1">More Examples:</p>
                        <div className="space-y-2">
                            <div>
                                <code className="block text-xs bg-surface-raised p-2 rounded">tools[*].metadata</code>
                                <p className="text-xs text-secondary mt-1">Remove metadata from all tools</p>
                            </div>
                            <div>
                                <code className="block text-xs bg-surface-raised p-2 rounded">messages[0].metadata.source</code>
                                <p className="text-xs text-secondary mt-1">Remove source field from first message's metadata</p>
                            </div>
                            <div>
                                <code className="block text-xs bg-surface-raised p-2 rounded">tool_choice.function.strict</code>
                                <p className="text-xs text-secondary mt-1">Remove strict mode from tool choice</p>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="mt-3 text-xs bg-yellow-50 border border-yellow-200 p-2 rounded">
                    ⚠️ <strong>Note:</strong> Nested field removal only works when fields are actually present in the request.
                    It won't cause errors if the field doesn't exist.
                </div>
            </div>
        </InfoBox>

        <InfoBox title="How It Works" icon={<Info className="w-5 h-5" />}>
            <div className="space-y-2 text-sm text-secondary">
                <p>
                    <strong>1. User makes request</strong> with parameters like temperature, top_p, etc.
                </p>
                <p>
                    <strong>2. The gateway removes</strong> the parameters on the model&apos;s own list, and those on the
                    provider&apos;s list when the switch is on, before forwarding
                </p>
                <p>
                    <strong>3. LLM receives only supported parameters</strong> - no errors!
                </p>
                <div className="mt-3 text-xs bg-blue-50 border border-blue-200 p-2 rounded">
                    💡 <strong>Tip:</strong> Copy the exact parameter list for your model type from the examples above
                </div>
            </div>
        </InfoBox>

        <InfoBox title="Best Practices" icon={<CheckCircle2 className="w-5 h-5" />}>
            <ul className="space-y-2 text-sm text-secondary">
                <li className="flex items-start space-x-2">
                    <CheckCircle2 className="w-4 h-4 text-green-600 flex-shrink-0 mt-0.5" />
                    <span>
                        <strong>Put a parameter every model of a provider rejects</strong> on the provider&apos;s list, once
                    </span>
                </li>
                <li className="flex items-start space-x-2">
                    <CheckCircle2 className="w-4 h-4 text-green-600 flex-shrink-0 mt-0.5" />
                    <span>
                        <strong>Test after configuration</strong> using the health check feature
                    </span>
                </li>
                <li className="flex items-start space-x-2">
                    <CheckCircle2 className="w-4 h-4 text-green-600 flex-shrink-0 mt-0.5" />
                    <span>
                        <strong>Document your choices</strong> in the model description field
                    </span>
                </li>
                <li className="flex items-start space-x-2">
                    <CheckCircle2 className="w-4 h-4 text-green-600 flex-shrink-0 mt-0.5" />
                    <span>
                        <strong>Keep lists minimal</strong> - only drop what's truly unsupported
                    </span>
                </li>
            </ul>
        </InfoBox>

        <InfoBox title="Important Notes" icon={<AlertCircle className="w-5 h-5" />}>
            <div className="space-y-2 text-sm text-secondary">
                <p>
                    <strong>The two lists add up:</strong> with the switch on, a parameter on either list is removed.
                    With the switch off, only the model&apos;s own list is.
                </p>
                <p>
                    <strong>The gateway removes only what is listed.</strong> It does not guess which parameters a
                    provider rejects: a parameter on neither list reaches the provider, which answers for it.
                </p>
            </div>
        </InfoBox>
    </div>
);

const RoutingHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">Routing & Monitoring</h3>
            <p className="text-sm text-secondary">
                Configure load balancing, rate limits, costs, and tracking.
            </p>
        </div>

        <InfoBox icon={<Share2 className="w-5 h-5" />} title="Routing Groups" variant="success">
            <p className="text-sm mb-3">
                Assign models to routing groups for intelligent load balancing and traffic distribution.
            </p>
            <div className="space-y-2 text-sm">
                <div className="bg-green-50 border border-green-200 rounded p-3">
                    <strong className="text-green-900">What are routing groups?</strong>
                    <p className="text-xs text-green-800 mt-2">
                        Routing groups enable load balancing across multiple models with the same capability.
                        Requests are intelligently distributed based on the selected routing strategy.
                    </p>
                </div>
                <div className="bg-purple-50 border border-purple-200 rounded p-3">
                    <strong className="text-purple-900">
                        <Layers className="w-3.5 h-3.5 inline mr-1" />
                        Virtual Models
                    </strong>
                    <p className="text-xs text-purple-800 mt-2">
                        When a routing group is created, a <strong>virtual AI model</strong> is automatically
                        generated and appears in the model list with a purple "Virtual" badge. End-users and
                        AI agents select this virtual model like any other — behind the scenes, the gateway routes
                        the request across the group's member models using the configured strategy.
                    </p>
                    <div className="mt-2 text-xs text-purple-700">
                        <strong>Key points:</strong>
                        <ul className="list-disc list-inside mt-1 space-y-0.5 ml-2">
                            <li>Virtual models have no API key or provider connection — they proxy to members</li>
                            <li>Health checks are skipped (health depends on member models)</li>
                            <li>Assign the virtual model to resource groups to control user access</li>
                            <li>Member models are dual-registered: accessible directly <em>and</em> via the group</li>
                        </ul>
                    </div>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Common use cases:</strong>
                    <div className="mt-2 space-y-1 text-xs">
                        <div>• <strong>High Availability:</strong> Distribute traffic across multiple GPT-4 models</div>
                        <div>• <strong>Cost Optimization:</strong> Route to cheaper models when possible</div>
                        <div>• <strong>Failover:</strong> Automatically switch to backup models if one fails</div>
                        <div>• <strong>A/B Testing:</strong> Compare different models with controlled traffic split</div>
                    </div>
                </div>
                <div className="bg-blue-50 border border-blue-200 rounded p-3">
                    <strong className="text-blue-900">How to set up:</strong>
                    <ol className="list-decimal list-inside text-xs text-blue-800 mt-2 space-y-1">
                        <li>Navigate to <strong>Load Balancing</strong> tab</li>
                        <li>Go to <strong>Routing Groups</strong> sub-tab</li>
                        <li>Click <strong>Create Routing Group</strong></li>
                        <li>Configure routing strategy (usage-based, latency-based, cost-based, etc.)</li>
                        <li>A virtual model is created automatically — assign it to resource groups as needed</li>
                    </ol>
                </div>
                <div className="bg-yellow-50 border border-yellow-200 rounded p-3">
                    <strong className="text-yellow-900">Important:</strong>
                    <p className="text-xs text-yellow-800 mt-1">
                        Models in the same routing group should have <strong>similar capabilities</strong>.
                        Don't group a text model with a vision model, or models with different context windows.
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Activity className="w-5 h-5" />} title="Routing Weight" variant="info">
            <p className="text-sm mb-3">
                Integer weight (≥ 0). Controls how much traffic this model receives in load
                balancing scenarios. <strong>Only consumed for selection by the
                Weighted Shuffle strategy</strong> (<code>simple-shuffle</code>) — other
                strategies (Queue-Aware, Rate-Limit Optimized, Fastest Response,
                Cheapest Healthy) pick by their own signal and use weight only as a
                tiebreaker / sort order.
            </p>
            <div className="space-y-2 text-sm">
                <div className="bg-blue-50 border border-blue-200 rounded p-3 dark:bg-blue-950/40 dark:border-blue-900">
                    <strong className="text-blue-900 dark:text-blue-200">How it works (Weighted Shuffle):</strong>
                    <p className="text-xs text-blue-800 dark:text-blue-300 mt-2">
                        If you have 3 models with weights <code>1</code>, <code>2</code>, and{' '}
                        <code>3</code>, they receive traffic in a ratio of 1:2:3 (≈17%, 33%, 50%).
                        Weights are integers — fractions are truncated to 0.
                    </p>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Common values:</strong>
                    <div className="mt-2 space-y-1 text-xs">
                        <div><code>1</code> — Standard weight (baseline)</div>
                        <div><code>2</code> — Double the traffic of a baseline peer</div>
                        <div><code>7</code> / <code>3</code> — ~70/30 split between two peers</div>
                        <div><code>1</code> / <code>0</code> — Primary-with-fallback. The zero-weight peer only serves when the weight-1 peer is in cooldown (because the strategy excludes cooling-down members from the pool, leaving the zero-weight one as the only eligible target).</div>
                    </div>
                </div>
                <div className="bg-surface-alt border border-default rounded p-3">
                    <strong className="text-default">Self-check:</strong>
                    <p className="text-xs text-secondary mt-1">
                        Check the routing group's selected <strong>Routing Strategy</strong>.
                        If it's not <code>Weighted Shuffle</code>, this knob is a tiebreaker
                        only — your traffic distribution is decided by the strategy's primary
                        signal (TPM/RPM headroom, queue depth, latency, or cost).
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Zap className="w-5 h-5" />} title="Rate Limits" variant="warning">
            <p className="text-sm mb-3">
                The gateway applies these limits to every call this model takes: a direct call, a call through a
                routing group, and a call that falls back to it. A call above a limit is refused.
            </p>
            <div className="space-y-2 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">RPM (Requests Per Minute):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Maximum requests per minute. Helps prevent quota exhaustion.
                    </p>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">TPM (Tokens Per Minute):</strong>
                    <p className="text-xs text-secondary mt-1">
                        Maximum tokens (input + output) per minute. More precise for cost control.
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<DollarSign className="w-5 h-5" />} title="Cost Tracking" variant="info">
            <p className="text-sm mb-3">
                Track costs per million tokens for accurate budget monitoring.
            </p>
            <div className="bg-surface border border-default rounded p-3 text-sm">
                <strong className="text-default">Example Costs (USD per 1M tokens):</strong>
                <div className="mt-2 space-y-2 text-xs">
                    <div className="flex justify-between">
                        <span>GPT-4o Input:</span>
                        <code>$2.50</code>
                    </div>
                    <div className="flex justify-between">
                        <span>GPT-4o Output:</span>
                        <code>$10.00</code>
                    </div>
                    <div className="flex justify-between border-t pt-2">
                        <span>Claude Sonnet 4 Input:</span>
                        <code>$3.00</code>
                    </div>
                    <div className="flex justify-between">
                        <span>Claude Sonnet 4 Output:</span>
                        <code>$15.00</code>
                    </div>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Shield className="w-5 h-5" />} title="User Tracking" variant="info">
            <div className="space-y-3 text-sm">
                <div className="bg-blue-50 border border-blue-200 rounded p-3">
                    <strong className="text-blue-900">User Identifier:</strong>
                    <p className="text-xs text-blue-800 mt-1">
                        Template for tracking end-users. Helps providers detect abuse.
                    </p>
                    <code className="block mt-2 text-xs">Example: user-{'{id}'}</code>
                </div>
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Safety Identifier:</strong>
                    <p className="text-xs text-secondary mt-1">
                        Tag for compliance and safety tracking. Useful for regulated industries.
                    </p>
                    <code className="block mt-2 text-xs">Example: hipaa-compliant</code>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<FileCode className="w-5 h-5" />} title="Custom Metadata" variant="info">
            <p className="text-sm mb-3">
                Attach custom metadata to all requests for logging and monitoring.
            </p>
            <div className="bg-surface-alt border border-default rounded p-3">
                <pre className="text-xs font-mono overflow-x-auto">
{`{
  "team": "ml-ops",
  "environment": "production",
  "project": "support-bot"
}`}
                </pre>
            </div>
        </InfoBox>

        <InfoBox icon={<AlertCircle className="w-5 h-5" />} title="Fallback Configuration" variant="warning">
            <div className="space-y-3 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Fallback Models:</strong>
                    <p className="text-xs text-secondary mt-1">
                        If this model fails, try these models in order. Helps ensure high availability.
                    </p>
                    <code className="block mt-2 text-xs">Example: model-id-2, model-id-3</code>
                </div>
                <div className="bg-blue-50 border border-blue-200 rounded p-3">
                    <strong className="text-blue-900">Context Window Fallback:</strong>
                    <p className="text-xs text-blue-800 mt-1">
                        Automatically use a model with larger context if input exceeds limit.
                    </p>
                </div>
            </div>
        </InfoBox>
    </div>
);

const SystemPromptHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-2">System Prompt Configuration</h3>
            <p className="text-sm text-secondary">
                Configure a default system prompt that establishes the AI's role, behavior, and guidelines for all interactions using this model.
            </p>
        </div>

        <InfoBox icon={<MessageSquare className="w-5 h-5" />} title="What is a System Prompt?" variant="info">
            <div className="space-y-3 text-sm">
                <p>
                    A <strong>system prompt</strong> is a special message that sets the context and instructions for the AI model. It defines:
                </p>
                <ul className="list-disc list-inside space-y-1 ml-2">
                    <li>The AI's role and personality (e.g., "helpful assistant", "expert coder")</li>
                    <li>Behavioral guidelines (e.g., "always be polite", "provide citations")</li>
                    <li>Output format preferences (e.g., "use bullet points", "keep answers concise")</li>
                    <li>Domain-specific knowledge or constraints</li>
                </ul>
                <p className="mt-3">
                    The system prompt is automatically prepended to every completion request as the first message with <code>role="system"</code>.
                </p>
            </div>
        </InfoBox>

        <InfoBox icon={<Lightbulb className="w-5 h-5" />} title="How It Works" variant="success">
            <div className="space-y-3 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <strong className="text-default">Automatic Injection:</strong>
                    <p className="text-xs text-secondary mt-1">
                        When configured, the system prompt is automatically added as the first message before all user messages:
                    </p>
                    <div className="mt-2 space-y-1 text-xs font-mono bg-surface-alt p-2 rounded">
                        <div>📝 <span className="text-green-600">System:</span> "You are a helpful assistant..."</div>
                        <div>👤 <span className="text-blue-600">User:</span> "What's the weather like?"</div>
                        <div>🤖 <span className="text-purple-600">Assistant:</span> "I'd be happy to help..."</div>
                    </div>
                </div>
                <div className="bg-blue-50 border border-blue-200 rounded p-3">
                    <strong className="text-blue-900">Scope:</strong>
                    <p className="text-xs text-blue-800 mt-1">
                        This system prompt applies to ALL requests made through this model configuration. Users cannot override it unless they provide their own system message in the request.
                    </p>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<Code className="w-5 h-5" />} title="Example Use Cases" variant="info">
            <div className="space-y-4 text-sm">
                <div>
                    <strong className="text-default block mb-2">1. Customer Support Bot</strong>
                    <div className="bg-surface-alt border border-default rounded p-3">
                        <pre className="text-xs font-mono overflow-x-auto whitespace-pre-wrap">
You are a friendly and professional customer support assistant for TechCorp.

Guidelines:
- Always be polite, empathetic, and solution-focused
- Use the customer's name when known
- If you don't know the answer, offer to escalate to a human agent
- Keep responses concise but helpful
- Never make promises about refunds or technical fixes without confirmation

Our business hours: Mon-Fri 9AM-5PM EST
Support email: support@techcorp.com
                        </pre>
                    </div>
                </div>

                <div>
                    <strong className="text-default block mb-2">2. Code Review Assistant</strong>
                    <div className="bg-surface-alt border border-default rounded p-3">
                        <pre className="text-xs font-mono overflow-x-auto whitespace-pre-wrap">
You are an expert code reviewer with deep knowledge of software engineering best practices.

When reviewing code:
- Check for security vulnerabilities (SQL injection, XSS, CSRF, etc.)
- Evaluate performance implications
- Assess code readability and maintainability
- Verify proper error handling
- Suggest improvements with specific examples
- Be constructive and educational in feedback
- Prioritize critical issues over style preferences

Always explain the "why" behind your recommendations.
                        </pre>
                    </div>
                </div>

                <div>
                    <strong className="text-default block mb-2">3. Content Writer for Blog</strong>
                    <div className="bg-surface-alt border border-default rounded p-3">
                        <pre className="text-xs font-mono overflow-x-auto whitespace-pre-wrap">
You are a professional content writer specializing in technology blog posts.

Writing Style:
- Clear, engaging, and accessible to non-technical readers
- Use active voice and concrete examples
- Keep paragraphs short (3-4 sentences max)
- Include relevant statistics and citations when available
- Use headers and bullet points for scannability

Tone: Professional yet conversational
Target audience: Tech-savvy professionals
Article length: 800-1200 words typical
                        </pre>
                    </div>
                </div>
            </div>
        </InfoBox>

        <InfoBox icon={<AlertCircle className="w-5 h-5" />} title="Best Practices" variant="warning">
            <div className="space-y-3 text-sm">
                <ul className="list-disc list-inside space-y-2 ml-2">
                    <li>
                        <strong>Be specific:</strong> Clearly define the AI's role and expected behavior
                    </li>
                    <li>
                        <strong>Set boundaries:</strong> Specify what the AI should and shouldn't do
                    </li>
                    <li>
                        <strong>Keep it focused:</strong> Avoid overly long prompts - aim for clarity over comprehensiveness
                    </li>
                    <li>
                        <strong>Test thoroughly:</strong> Validate that the prompt produces desired behavior across different queries
                    </li>
                    <li>
                        <strong>Avoid sensitive data:</strong> Don't include API keys, passwords, or confidential information
                    </li>
                    <li>
                        <strong>Consider token costs:</strong> Longer prompts consume more tokens on every request
                    </li>
                </ul>
            </div>
        </InfoBox>

        <InfoBox icon={<Info className="w-5 h-5" />} title="Technical Details" variant="info">
            <div className="space-y-3 text-sm">
                <div className="bg-surface border border-default rounded p-3">
                    <div className="space-y-2">
                        <div className="flex justify-between">
                            <span className="font-medium">Maximum Length:</span>
                            <code>100,000 characters</code>
                        </div>
                        <div className="flex justify-between">
                            <span className="font-medium">Encoding:</span>
                            <code>UTF-8 (supports all languages and emoji)</code>
                        </div>
                        <div className="flex justify-between">
                            <span className="font-medium">Applied When:</span>
                            <code>On every completion request</code>
                        </div>
                        <div className="flex justify-between">
                            <span className="font-medium">Override:</span>
                            <code>User system messages take precedence</code>
                        </div>
                    </div>
                </div>
                <p className="text-xs text-secondary">
                    💡 The system prompt is stored securely and sent to the model provider with each request. It counts toward your total token usage.
                </p>
            </div>
        </InfoBox>

        <InfoBox icon={<CheckCircle2 className="w-5 h-5" />} title="When to Use System Prompts" variant="success">
            <div className="space-y-3 text-sm">
                <p>System prompts are ideal when you need:</p>
                <ul className="list-disc list-inside space-y-1 ml-2">
                    <li>✅ Consistent behavior across all interactions with a model</li>
                    <li>✅ Specific role definition (customer support, code assistant, etc.)</li>
                    <li>✅ Compliance or safety guidelines that must always apply</li>
                    <li>✅ Brand voice or communication style enforcement</li>
                    <li>✅ Domain-specific knowledge or context</li>
                </ul>
                <p className="mt-3">Leave empty if:</p>
                <ul className="list-disc list-inside space-y-1 ml-2">
                    <li>❌ You want maximum flexibility for different use cases</li>
                    <li>❌ Users will provide their own system prompts per request</li>
                    <li>❌ The model will be used for diverse, unrelated tasks</li>
                </ul>
            </div>
        </InfoBox>
    </div>
);

export default LLModelFormHelp;