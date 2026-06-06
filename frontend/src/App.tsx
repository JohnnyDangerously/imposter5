import { useState, useEffect, useRef } from 'react';
import {
  Play,
  Pause,
  Volume2,
  VolumeX,
  Maximize,
  Clock,
  Compass,
  MousePointer,
  Eye,
  ChevronRight,
  Sparkles,
  Info,
  ExternalLink,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Terminal as TerminalIcon,
  Sliders,
  Settings,
  Globe,
  Video,
  Activity,
  CheckSquare,
  Square,
  RefreshCw,
  Plus,
  Trash2,
  X,
} from 'lucide-react';

interface Persona {
  name: string;
  patience: number;
  scroll_style: string;
  interaction_style: string;
}

interface Imposter5Result {
  success: boolean;
  plan: any;
  goal?: {
    name: string;
    start_url: string;
    desired_outcome: string;
    prompt: string;
    steps: Array<{
      name: string;
      action: string;
      required: boolean;
      params?: any;
    }>;
  } | null;
  movie_filename: string;
  movie_url: string;
  stamped_codex_path: string;
  latest_codex_path: string;
  bot_likeness_score: number | null;
  verdict: string | null;
  real_verdict: {
    predicted_label: string;
    confidence: number;
    all_proba?: number[];
  } | null;
  logs: string[];
  session_recording?: {
    run_id: string;
    enabled: boolean;
    event_count: number;
    events: Array<{
      index: number;
      action: string;
      status: string;
      label: string;
      elapsed_ms: number;
      metadata: any;
    }>;
  } | null;
}

export default function App() {
  const [url, setUrl] = useState('https://en.wikipedia.org/wiki/Artificial_intelligence');
  const [provider, setProvider] = useState<'generic' | 'linkedin'>('generic');
  const [prompt, setPrompt] = useState('');
  const [persona, setPersona] = useState('curious_reader');
  const [completion, setCompletion] = useState('skim_visible_feed');
  const [runFpAgent, setRunFpAgent] = useState(true);
  const [personas, setPersonas] = useState<Persona[]>([]);

  // Techniques / Variations
  const [variations, setVariations] = useState({
    bidirectional_scroll: true,
    hover_and_read: true,
    use_markov_pathing: false,
    expand_comments: true,
    profile_peeks: false,
    notifications_check: false,
    avatar_or_picture_clicks: false,
  });

  // Custom Human Config (Mouse Physics)
  const [showPhysics, setShowPhysics] = useState(false);
  const [humanConfig, setHumanConfig] = useState({
    mouse_wobble_max: 5.5,
    mouse_max_steps: 140,
    mouse_overshoot_chance: 0.32,
    mouse_overshoot_px_min: 3,
    mouse_overshoot_px_max: 13,
    mouse_burst_size_min: 2,
    mouse_burst_size_max: 8,
    mouse_burst_pause_min: 4,
    mouse_burst_pause_max: 28,
    click_aim_delay_button_min: 40,
    click_aim_delay_button_max: 210,
  });

  const [loading, setLoading] = useState(false);
  const [simLogs, setSimLogs] = useState<string[]>([]);
  const [result, setResult] = useState<Imposter5Result | null>(null);
  const [error, setError] = useState<string | null>(null);

  interface Website {
    name: string;
    url: string;
    description: string;
  }

  const [websites, setWebsites] = useState<Website[]>([]);
  const [selectedWebsite, setSelectedWebsite] = useState<string>('');
  const [showWebsiteModal, setShowWebsiteModal] = useState(false);
  const [newWebsite, setNewWebsite] = useState({ name: '', url: '', description: '' });

  const [showPersonaModal, setShowPersonaModal] = useState(false);
  const [newPersona, setNewPersona] = useState({
    name: '',
    patience: 'medium',
    scroll_style: 'pause_and_read',
    dwell_multiplier: 1.0,
    scroll_multiplier: 1.0,
    interaction_style: 'low_touch'
  });

  const fetchWebsites = async () => {
    try {
      const res = await fetch('/api/imposter5/websites');
      if (res.ok) {
        const data = await res.json();
        if (data.ok && data.websites) {
          setWebsites(data.websites);
          const wiki = data.websites.find((w: Website) => w.name === 'Wikipedia AI');
          if (wiki) {
            setSelectedWebsite(wiki.name);
            setUrl(wiki.url);
          } else if (data.websites.length > 0) {
            setSelectedWebsite(data.websites[0].name);
            setUrl(data.websites[0].url);
          }
        }
      }
    } catch (err) {
      console.error('Failed to load websites:', err);
    }
  };

  const fetchPersonas = async () => {
    try {
      const res = await fetch('/api/imposter5/personas');
      if (res.ok) {
        const data = await res.json();
        if (data.ok && data.personas) {
          setPersonas(data.personas);
        }
      }
    } catch (err) {
      console.error('Failed to load personas:', err);
    }
  };

  useEffect(() => {
    fetchWebsites();
    fetchPersonas();
  }, []);

  const handleSaveWebsite = async () => {
    if (!newWebsite.name || !newWebsite.url) return;
    try {
      const res = await fetch('/api/imposter5/websites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newWebsite)
      });
      if (res.ok) {
        await fetchWebsites();
        setSelectedWebsite(newWebsite.name);
        setUrl(newWebsite.url);
        setShowWebsiteModal(false);
        setNewWebsite({ name: '', url: '', description: '' });
      }
    } catch (err) {
      console.error('Failed to save website:', err);
    }
  };

  const handleDeleteWebsite = async (name: string) => {
    try {
      const res = await fetch(`/api/imposter5/websites/${encodeURIComponent(name)}`, {
        method: 'DELETE'
      });
      if (res.ok) {
        await fetchWebsites();
      }
    } catch (err) {
      console.error('Failed to delete website:', err);
    }
  };

  const handleSavePersona = async () => {
    if (!newPersona.name) return;
    try {
      const res = await fetch('/api/imposter5/personas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newPersona)
      });
      if (res.ok) {
        await fetchPersonas();
        setPersona(newPersona.name);
        setShowPersonaModal(false);
        setNewPersona({
          name: '',
          patience: 'medium',
          scroll_style: 'pause_and_read',
          dwell_multiplier: 1.0,
          scroll_multiplier: 1.0,
          interaction_style: 'low_touch'
        });
      }
    } catch (err) {
      console.error('Failed to save persona:', err);
    }
  };

  const handleDeletePersona = async (name: string) => {
    try {
      const res = await fetch(`/api/imposter5/personas/${encodeURIComponent(name)}`, {
        method: 'DELETE'
      });
      if (res.ok) {
        await fetchPersonas();
        if (persona === name) {
          setPersona('curious_reader');
        }
      }
    } catch (err) {
      console.error('Failed to delete persona:', err);
    }
  };

  const toggleVariation = (key: keyof typeof variations) => {
    setVariations((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const handlePhysicsChange = (key: keyof typeof humanConfig, val: number) => {
    setHumanConfig((prev) => ({ ...prev, [key]: val }));
  };

  const runSimulation = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    setSimLogs(['[SYSTEM] Initializing imposter5 simulation context...', '[SYSTEM] Preparing behavior pack and technique profiles...']);

    const mockLogInterval = setInterval(() => {
      const mockLogs = [
        '⚙️ [CLOAK] Launching headed Chromium instance with stealth context...',
        '💉 [INJECT] Injecting synthetic cursor overlay (__human_cursor__)...',
        '🎯 [CALIBRATE] Executing 6-point glide calibration path...',
        '🚀 [NAVIGATION] Navigating to target site...',
        '🖱️ [PRIMITIVES] Driving humanized arcs and two-step curves...',
        '📜 [SCROLL] Executing mouse-positioned wheel scrolls...',
        '🔬 [DETECTOR] Recording behavioral frames via mus.js...',
      ];
      const randomLog = mockLogs[Math.floor(Math.random() * mockLogs.length)] || '';
      setSimLogs((prev) => [...prev, randomLog]);
    }, 3000);

    try {
      const formattedConfig = {
        mouse_wobble_max: humanConfig.mouse_wobble_max,
        mouse_max_steps: humanConfig.mouse_max_steps,
        mouse_overshoot_chance: humanConfig.mouse_overshoot_chance,
        mouse_overshoot_px: [humanConfig.mouse_overshoot_px_min, humanConfig.mouse_overshoot_px_max],
        mouse_burst_size: [humanConfig.mouse_burst_size_min, humanConfig.mouse_burst_size_max],
        mouse_burst_pause: [humanConfig.mouse_burst_pause_min, humanConfig.mouse_burst_pause_max],
        click_aim_delay_button: [humanConfig.click_aim_delay_button_min, humanConfig.click_aim_delay_button_max],
      };

      const response = await fetch('/api/imposter5/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url,
          provider,
          prompt: prompt || undefined,
          persona,
          completion,
          variations,
          human_config: formattedConfig,
          run_fp_agent: runFpAgent,
        }),
      });

      clearInterval(mockLogInterval);

      if (!response.ok) {
        throw new Error(`Server returned status ${response.status}`);
      }

      const data = await response.json();
      if (data.ok) {
        setResult(data);
        setSimLogs(data.logs || []);
      } else {
        throw new Error(data.error || 'Simulation failed to complete.');
      }
    } catch (err: any) {
      clearInterval(mockLogInterval);
      setError(err.message || 'Simulation failed.');
      setSimLogs((prev) => [...prev, `❌ [ERROR] Simulation aborted: ${err.message}`]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen w-full bg-slate-950 text-slate-100 p-6 font-sans">
      <div className="max-w-[1720px] mx-auto flex flex-col gap-6">
        {/* Header */}
        <div className="flex flex-col md:flex-row md:items-center justify-between border-b border-rose-500/30 pb-4">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-rose-500/10 border border-rose-500 flex items-center justify-center shadow-[0_0_15px_rgba(244,63,94,0.4)]">
              <ShieldAlert className="h-6 w-6 text-rose-500 animate-pulse" />
            </div>
            <div>
              <h1 className="text-2xl font-black tracking-wider text-transparent bg-clip-text bg-gradient-to-r from-rose-500 via-purple-500 to-cyan-400">
                IMPOSTER5
              </h1>
              <p className="text-xs text-slate-400 tracking-widest uppercase">Red Team Human Mechanics Simulation & Evasion Suite</p>
            </div>
          </div>
          <div className="mt-4 md:mt-0 flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-emerald-500 animate-ping" />
            <span className="text-xs font-mono text-emerald-400 tracking-wider">STANDALONE DEPLOYMENT // PORT 5185</span>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          {/* Left Column: Config Panel */}
          <div className="lg:col-span-5 flex flex-col gap-6">
            {/* Target Website Card */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
              <div className="flex justify-between items-center mb-4">
                <h2 className="text-sm font-bold uppercase tracking-wider text-rose-400 flex items-center gap-2">
                  <Globe className="h-4 w-4" /> 1. Model Target Website
                </h2>
                <button
                  type="button"
                  onClick={() => setShowWebsiteModal(true)}
                  className="flex items-center gap-1 text-[10px] uppercase font-bold tracking-wider px-2 py-1 rounded bg-rose-500/10 border border-rose-500/30 text-rose-400 hover:bg-rose-500/20 transition-all"
                >
                  <Plus className="h-3 w-3" /> Save Current Site
                </button>
              </div>

              <div className="flex flex-col gap-4">
                <div>
                  <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Saved Websites</label>
                  <div className="flex gap-2">
                    <select
                      value={selectedWebsite}
                      onChange={(e) => {
                        const name = e.target.value;
                        setSelectedWebsite(name);
                        const match = websites.find((w) => w.name === name);
                        if (match) {
                          setUrl(match.url);
                          if (match.url.includes('linkedin.com')) {
                            setProvider('linkedin');
                          } else {
                            setProvider('generic');
                          }
                        }
                      }}
                      className="flex-1 bg-slate-950 border border-slate-800 rounded-lg py-2 px-2.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-rose-500/50"
                    >
                      <option value="">-- Select a Saved Website --</option>
                      {websites.map((w) => (
                        <option key={w.name} value={w.name}>
                          {w.name}
                        </option>
                      ))}
                    </select>
                    {selectedWebsite && !['LinkedIn Feed', 'Wikipedia AI', 'Wikipedia Machine Learning', 'Yahoo News', 'Hacker News'].includes(selectedWebsite) && (
                      <button
                        type="button"
                        onClick={() => handleDeleteWebsite(selectedWebsite)}
                        className="p-2 rounded-lg bg-slate-950 border border-slate-800 text-slate-500 hover:border-rose-500/50 hover:text-rose-400 transition-all"
                        title="Delete selected website"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                  {selectedWebsite && (
                    <p className="text-[10px] text-slate-400 mt-1.5 italic">
                      {websites.find((w) => w.name === selectedWebsite)?.description}
                    </p>
                  )}
                </div>

                <div className="grid grid-cols-1 md:grid-cols-12 gap-4 items-end">
                  <div className="md:col-span-8">
                    <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Target URL</label>
                    <input
                      type="text"
                      value={url}
                      onChange={(e) => {
                        const val = e.target.value;
                        setUrl(val);
                        if (val.includes('linkedin.com')) {
                          setProvider('linkedin');
                        } else {
                          setProvider('generic');
                        }
                      }}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-xs font-mono text-slate-200 focus:outline-none focus:border-rose-500/50"
                      placeholder="https://example.com"
                    />
                  </div>

                  <div className="md:col-span-4">
                    <div className="flex flex-col gap-1.5">
                      <span className="text-[10px] font-mono text-slate-400 uppercase">Active Protocol</span>
                      <div className={`py-2 px-3 rounded-lg border text-xs font-bold text-center transition-all ${
                        provider === 'linkedin'
                          ? 'bg-rose-500/10 border-rose-500 text-rose-400 shadow-[0_0_10px_rgba(244,63,94,0.15)]'
                          : 'bg-cyan-500/10 border-cyan-500 text-cyan-400 shadow-[0_0_10px_rgba(34,211,238,0.15)]'
                      }`}>
                        {provider === 'linkedin' ? 'LinkedIn Feed' : 'Generic Web'}
                      </div>
                    </div>
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Custom Mission Prompt (Optional)</label>
                  <textarea
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-xs font-mono text-slate-200 focus:outline-none focus:border-rose-500/50 h-16 resize-none"
                    placeholder="e.g. click 3 links on the page, wait 5 seconds, scroll down..."
                  />
                </div>
              </div>
            </div>

            {/* Behavior Pack Card */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
              <div className="flex justify-between items-center mb-4">
                <h2 className="text-sm font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
                  <Activity className="h-4 w-4" /> 2. Assign Behavior Pack
                </h2>
                <button
                  type="button"
                  onClick={() => setShowPersonaModal(true)}
                  className="flex items-center gap-1 text-[10px] uppercase font-bold tracking-wider px-2 py-1 rounded bg-purple-500/10 border border-purple-500/30 text-purple-400 hover:bg-purple-500/20 transition-all"
                >
                  <Plus className="h-3 w-3" /> Create Pack
                </button>
              </div>

              <div className="flex flex-col gap-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Persona Profile</label>
                    <div className="flex gap-2">
                      <select
                        value={persona}
                        onChange={(e) => setPersona(e.target.value)}
                        className="flex-1 bg-slate-950 border border-slate-800 rounded-lg py-2 px-2 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500/50"
                      >
                        {personas.map((p) => (
                          <option key={p.name} value={p.name}>
                            {p.name.replace(/_/g, ' ')}
                          </option>
                        ))}
                      </select>
                      {persona && !['focused_power_user', 'curious_reader', 'impatient_scanner', 'slow_reader', 'methodical_operator', 'mobile_checker', 'late_day_review', 'naive_bot'].includes(persona) && (
                        <button
                          type="button"
                          onClick={() => handleDeletePersona(persona)}
                          className="p-2 rounded-lg bg-slate-950 border border-slate-800 text-slate-500 hover:border-rose-500/50 hover:text-rose-400 transition-all"
                          title="Delete custom persona"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      )}
                    </div>
                  </div>

                  <div>
                    <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Completion Depth</label>
                    <select
                      value={completion}
                      onChange={(e) => setCompletion(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-2.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500/50"
                    >
                      <option value="skim_visible_feed">Skim Visible Feed</option>
                      <option value="glance_only">Glance Only</option>
                      <option value="review_feed">Review Feed</option>
                      <option value="deep_review_feed">Deep Review Feed</option>
                    </select>
                  </div>
                </div>

                {/* Display active persona details */}
                {personas.find((p) => p.name === persona) && (
                  <div className="bg-slate-950/80 border border-slate-800/80 rounded-lg p-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px] font-mono text-slate-400">
                    <div className="flex justify-between">
                      <span>Patience:</span>
                      <span className="text-purple-400 font-bold capitalize">{personas.find((p) => p.name === persona)?.patience}</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Interaction:</span>
                      <span className="text-purple-400 font-bold capitalize">{personas.find((p) => p.name === persona)?.interaction_style?.replace(/_/g, ' ')}</span>
                    </div>
                    <div className="flex justify-between col-span-2 border-t border-slate-900 pt-1.5 mt-1">
                      <span>Scroll Style:</span>
                      <span className="text-cyan-400 font-bold capitalize">{personas.find((p) => p.name === persona)?.scroll_style?.replace(/_/g, ' ')}</span>
                    </div>
                  </div>
                )}

                <div className="border-t border-slate-800/60 pt-4">
                  <label className="block text-xs font-mono text-slate-400 mb-3 uppercase">Active Engagement Techniques</label>
                  
                  {/* Generic Techniques */}
                  <div className="mb-4">
                    <span className="text-[10px] font-mono text-slate-500 uppercase tracking-wider block mb-2">Generic Browsing Techniques (All Sites)</span>
                    <div className="grid grid-cols-2 gap-2">
                      {['bidirectional_scroll', 'hover_and_read', 'use_markov_pathing'].map((key) => {
                        const active = variations[key as keyof typeof variations];
                        return (
                          <button
                            key={key}
                            type="button"
                            onClick={() => toggleVariation(key as keyof typeof variations)}
                            className={`flex items-center gap-2 py-2 px-3 rounded-lg border text-left text-xs transition-all ${
                              active
                                ? 'bg-purple-500/10 border-purple-500 text-purple-300'
                                : 'bg-slate-950 border-slate-800 text-slate-500 hover:border-slate-700'
                            }`}
                          >
                            {active ? (
                              <CheckSquare className="h-3.5 w-3.5 text-purple-400 shrink-0" />
                            ) : (
                              <Square className="h-3.5 w-3.5 text-slate-600 shrink-0" />
                            )}
                            <span className="truncate capitalize">{key.replace(/_/g, ' ')}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {/* LinkedIn Specific Techniques */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[10px] font-mono text-slate-500 uppercase tracking-wider block">LinkedIn-Specific Feed Actions</span>
                      {provider !== 'linkedin' && (
                        <span className="text-[9px] font-mono text-rose-400 uppercase tracking-wider bg-rose-950/40 border border-rose-900/50 px-1.5 py-0.5 rounded">
                          Inactive for Generic Web
                        </span>
                      )}
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {['expand_comments', 'profile_peeks', 'notifications_check', 'avatar_or_picture_clicks'].map((key) => {
                        const active = variations[key as keyof typeof variations] && provider === 'linkedin';
                        return (
                          <button
                            key={key}
                            type="button"
                            disabled={provider !== 'linkedin'}
                            onClick={() => toggleVariation(key as keyof typeof variations)}
                            className={`flex items-center gap-2 py-2 px-3 rounded-lg border text-left text-xs transition-all ${
                              provider !== 'linkedin'
                                ? 'bg-slate-950/40 border-slate-900/40 text-slate-600 cursor-not-allowed'
                                : active
                                ? 'bg-rose-500/10 border-rose-500 text-rose-300 shadow-[0_0_10px_rgba(244,63,94,0.1)]'
                                : 'bg-slate-950 border-slate-800 text-slate-500 hover:border-slate-700'
                            }`}
                          >
                            {provider !== 'linkedin' ? (
                              <Square className="h-3.5 w-3.5 text-slate-800 shrink-0" />
                            ) : active ? (
                              <CheckSquare className="h-3.5 w-3.5 text-rose-400 shrink-0" />
                            ) : (
                              <Square className="h-3.5 w-3.5 text-slate-600 shrink-0" />
                            )}
                            <span className="truncate capitalize">{key.replace(/_/g, ' ')}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Custom Mouse Physics (Human Config) */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
              <button
                type="button"
                onClick={() => setShowPhysics(!showPhysics)}
                className="w-full flex items-center justify-between text-sm font-bold uppercase tracking-wider text-cyan-400 focus:outline-none"
              >
                <span className="flex items-center gap-2">
                  <Sliders className="h-4 w-4" /> 3. Custom Mouse Physics (Human Config)
                </span>
                <Settings className={`h-4 w-4 transition-transform ${showPhysics ? 'rotate-45 text-cyan-400' : 'text-slate-500'}`} />
              </button>

              {showPhysics && (
                <div className="flex flex-col gap-4 mt-4 border-t border-slate-800/50 pt-4">
                  <div>
                    <div className="flex justify-between text-xs font-mono text-slate-400 mb-1">
                      <span>Mouse Wobble Max (px)</span>
                      <span className="text-cyan-400">{humanConfig.mouse_wobble_max}</span>
                    </div>
                    <input
                      type="range"
                      min="1"
                      max="15"
                      step="0.5"
                      value={humanConfig.mouse_wobble_max}
                      onChange={(e) => handlePhysicsChange('mouse_wobble_max', parseFloat(e.target.value))}
                      className="w-full accent-cyan-500 bg-slate-950 h-1 rounded-lg appearance-none cursor-pointer"
                    />
                  </div>

                  <div>
                    <div className="flex justify-between text-xs font-mono text-slate-400 mb-1">
                      <span>Mouse Max Steps</span>
                      <span className="text-cyan-400">{humanConfig.mouse_max_steps}</span>
                    </div>
                    <input
                      type="range"
                      min="50"
                      max="300"
                      step="10"
                      value={humanConfig.mouse_max_steps}
                      onChange={(e) => handlePhysicsChange('mouse_max_steps', parseInt(e.target.value))}
                      className="w-full accent-cyan-500 bg-slate-950 h-1 rounded-lg appearance-none cursor-pointer"
                    />
                  </div>

                  <div>
                    <div className="flex justify-between text-xs font-mono text-slate-400 mb-1">
                      <span>Overshoot Chance (%)</span>
                      <span className="text-cyan-400">{Math.round(humanConfig.mouse_overshoot_chance * 100)}%</span>
                    </div>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.05"
                      value={humanConfig.mouse_overshoot_chance}
                      onChange={(e) => handlePhysicsChange('mouse_overshoot_chance', parseFloat(e.target.value))}
                      className="w-full accent-cyan-500 bg-slate-950 h-1 rounded-lg appearance-none cursor-pointer"
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs font-mono text-slate-400 mb-1">Overshoot Px Min</label>
                      <input
                        type="number"
                        value={humanConfig.mouse_overshoot_px_min}
                        onChange={(e) => handlePhysicsChange('mouse_overshoot_px_min', parseInt(e.target.value))}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg py-1 px-2.5 text-xs font-mono text-slate-200 focus:outline-none"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-mono text-slate-400 mb-1">Overshoot Px Max</label>
                      <input
                        type="number"
                        value={humanConfig.mouse_overshoot_px_max}
                        onChange={(e) => handlePhysicsChange('mouse_overshoot_px_max', parseInt(e.target.value))}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg py-1 px-2.5 text-xs font-mono text-slate-200 focus:outline-none"
                    />
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Run Control */}
            <div className="flex flex-col gap-3">
              {error && (
                <div className="rounded-lg border border-rose-500/50 bg-rose-950/30 p-3 text-xs font-mono text-rose-400">
                  ❌ {error}
                </div>
              )}
              <div className="flex items-center justify-between px-1">
                <label className="flex items-center gap-2 text-xs font-mono text-slate-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={runFpAgent}
                    onChange={(e) => setRunFpAgent(e.target.checked)}
                    className="rounded border-slate-800 bg-slate-950 text-rose-500 focus:ring-rose-500/30"
                  />
                  Score against fp-agent protocol afterwards
                </label>
              </div>

              <button
                type="button"
                disabled={loading}
                onClick={runSimulation}
                className="w-full py-3 px-4 rounded-xl font-black tracking-widest uppercase bg-gradient-to-r from-rose-500 via-purple-600 to-cyan-500 text-white hover:opacity-90 disabled:opacity-50 transition-all shadow-[0_0_20px_rgba(244,63,94,0.3)] flex items-center justify-center gap-2"
              >
                {loading ? (
                  <>
                    <RefreshCw className="h-5 w-5 animate-spin" />
                    SIMULATING IMPOSTER5...
                  </>
                ) : (
                  <>
                    <Play className="h-5 w-5 fill-current" />
                    LAUNCH IMPOSTER5 SESSION
                  </>
                )}
              </button>
            </div>
          </div>

          {/* Right Column: Visualizer & Results */}
          <div className="lg:col-span-7 flex flex-col gap-6">
            {/* Simulation Output / Video */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)] flex-1 flex flex-col min-h-[400px]">
              <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 mb-4 flex items-center gap-2">
                <Video className="h-4 w-4" /> Visual Watch & Capture
              </h3>

              {loading ? (
                <div className="flex-1 flex flex-col items-center justify-center border border-slate-800/80 bg-slate-950 rounded-lg p-4 relative overflow-hidden">
                  <div className="flex flex-col items-center gap-4 z-10 text-center">
                    <div className="relative h-16 w-16">
                      <div className="absolute inset-0 rounded-full border-4 border-rose-500/20 border-t-rose-500 animate-spin" />
                      <div className="absolute inset-2 rounded-full border-4 border-cyan-500/20 border-b-cyan-500 animate-spin [animation-direction:reverse]" />
                    </div>
                    <div>
                      <p className="text-sm font-mono text-rose-400 animate-pulse tracking-wide font-bold">IMPOSTER5 ACTIVE IN DESKTOP SESSION</p>
                      <p className="text-xs text-slate-500 mt-1">A headed browser window has popped. Recording movie with red cursor overlay...</p>
                    </div>
                  </div>
                </div>
              ) : result?.movie_url ? (
                <LoomPlayer
                  movieUrl={result.movie_url}
                  movieFilename={result.movie_filename}
                  sessionRecording={result.session_recording}
                />
              ) : (
                <div className="flex-1 flex flex-col items-center justify-center border border-slate-800/80 bg-slate-950 rounded-lg p-4 relative overflow-hidden">
                  <div className="flex flex-col items-center gap-3 text-slate-500 text-center max-w-sm">
                    <Video className="h-12 w-12 text-slate-700 animate-pulse" />
                    <p className="text-sm font-bold">No Active Simulation</p>
                    <p className="text-xs">Configure your techniques and click "Launch Imposter5 Session" above. A headed browser will pop up and record the visual mechanics with the red HUMAN MOUSE cursor.</p>
                  </div>
                </div>
              )}
            </div>

            {/* Evasion & Verdict Results */}
            {result && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {/* Heuristic Verdict */}
                <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Heuristic Evasion Score</h3>
                  <div className="flex items-center gap-4">
                    <div className="relative h-16 w-16 shrink-0 flex items-center justify-center">
                      <svg className="absolute inset-0 w-full h-full -rotate-90">
                        <circle cx="32" cy="32" r="28" className="stroke-slate-800 fill-none" strokeWidth="4" />
                        <circle
                          cx="32"
                          cy="32"
                          r="28"
                          className={`fill-none transition-all duration-1000 ${
                            result.bot_likeness_score !== null && result.bot_likeness_score < 0.55
                              ? 'stroke-emerald-500'
                              : 'stroke-rose-500'
                          }`}
                          strokeWidth="4"
                          strokeDasharray={175}
                          strokeDashoffset={175 - (175 * (result.bot_likeness_score ?? 0))}
                        />
                      </svg>
                      <span className="text-sm font-mono font-bold">
                        {result.bot_likeness_score !== null ? Math.round(result.bot_likeness_score * 100) : 'N/A'}%
                      </span>
                    </div>
                    <div>
                      <div className="flex items-center gap-1.5">
                        {result.bot_likeness_score !== null && result.bot_likeness_score < 0.55 ? (
                          <div className="flex items-center gap-1 text-emerald-400 text-sm font-black tracking-wide">
                            <ShieldCheck className="h-4 w-4" /> EVADES DETECTOR
                          </div>
                        ) : (
                          <div className="flex items-center gap-1 text-rose-400 text-sm font-black tracking-wide">
                            <ShieldAlert className="h-4 w-4" /> DETECTED AS BOT
                          </div>
                        )}
                      </div>
                      <p className="text-xs text-slate-400 mt-1">
                        {result.bot_likeness_score !== null && result.bot_likeness_score < 0.55
                          ? 'Low bot-likeness. Trajectories are curved, variable, and mimic human timing.'
                          : 'High bot-likeness. Trajectories are too straight or timing is too regular.'}
                      </p>
                    </div>
                  </div>
                </div>

                {/* Real Model Verdict */}
                <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Real fp-agent Model Verdict</h3>
                  {result.real_verdict ? (
                    <div className="flex flex-col gap-2">
                      <div className="flex justify-between items-center border-b border-slate-800 pb-2">
                        <span className="text-xs font-mono text-slate-400">Predicted Cluster</span>
                        <span className="text-sm font-mono font-bold text-rose-400">{result.real_verdict.predicted_label}</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-xs font-mono text-slate-400">Model Confidence</span>
                        <span className="text-sm font-mono font-bold text-cyan-400">
                          {Math.round(result.real_verdict.confidence * 100)}%
                        </span>
                      </div>
                      <p className="text-[10px] text-slate-500 font-mono mt-1 leading-normal">
                        The XGBoost behavioral classifier mapped your mus.js movement frames to the '{result.real_verdict.predicted_label}' cluster.
                      </p>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center justify-center h-16 text-slate-500">
                      <Shield className="h-5 w-5 text-slate-700 mb-1" />
                      <span className="text-xs font-mono">No model verdict returned</span>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Interpreted Goal & Steps */}
            {result?.goal && (
              <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
                <h3 className="text-xs font-bold uppercase tracking-wider text-cyan-400 mb-3 flex items-center gap-1.5">
                  <CheckSquare className="h-4 w-4" /> Interpreted Goal & Steps
                </h3>
                <div className="flex flex-col gap-3 font-mono text-xs">
                  <div className="bg-slate-950 border border-slate-800/80 rounded-lg p-3 flex flex-col gap-1.5">
                    <div className="flex justify-between border-b border-slate-800 pb-1.5">
                      <span className="text-slate-400">Goal Name:</span>
                      <span className="text-rose-400 font-bold">{result.goal.name}</span>
                    </div>
                    {result.goal.prompt && (
                      <div className="flex flex-col gap-1 border-b border-slate-800 pb-1.5">
                        <span className="text-slate-400">Interpreted Prompt:</span>
                        <span className="text-slate-300 italic">"{result.goal.prompt}"</span>
                      </div>
                    )}
                    <div className="flex justify-between">
                      <span className="text-slate-400">Desired Outcome:</span>
                      <span className="text-emerald-400">{result.goal.desired_outcome}</span>
                    </div>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <span className="text-slate-400 text-[10px] uppercase tracking-wider">Compiled Execution Steps:</span>
                    <div className="max-h-48 overflow-y-auto bg-slate-950 border border-slate-800/80 rounded-lg p-3 flex flex-col gap-2">
                      {result.goal.steps.map((step, idx) => (
                        <div key={idx} className="flex items-start justify-between gap-4 text-[11px] border-b border-slate-900 pb-1.5 last:border-0 last:pb-0">
                          <div className="flex items-center gap-2">
                            <span className="text-slate-500">{idx + 1}.</span>
                            <span className="text-purple-400 font-bold">{step.name}</span>
                          </div>
                          <div className="flex items-center gap-2 text-right">
                            <span className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[10px] text-slate-400 uppercase">{step.action}</span>
                            {step.params?.selector && (
                              <span className="text-slate-500 text-[10px] truncate max-w-[150px]" title={step.params.selector}>
                                sel: {step.params.selector}
                              </span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Terminal Console */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)] flex-1 min-h-[180px] flex flex-col">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-1.5">
                <TerminalIcon className="h-3.5 w-3.5 text-rose-500" /> Live Simulation Console
              </h3>
              <div className="flex-1 bg-slate-950 border border-slate-800/80 rounded-lg p-3 font-mono text-[11px] text-emerald-400 overflow-y-auto h-40 flex flex-col gap-1 shadow-[inset_0_2px_8px_rgba(0,0,0,0.8)]">
                {simLogs.map((log, idx) => (
                  <div key={idx} className="leading-relaxed whitespace-pre-wrap">
                    {log}
                  </div>
                ))}
                {loading && <div className="text-rose-400 animate-pulse">▋ SIMULATING...</div>}
              </div>
            </div>
          </div>
        </div>

        {/* Save Website Modal */}
        {showWebsiteModal && (
          <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center z-50 p-4">
            <div className="bg-slate-900 border border-rose-500/40 rounded-xl p-6 max-w-md w-full shadow-[0_0_30px_rgba(244,63,94,0.2)]">
              <div className="flex justify-between items-center border-b border-slate-800 pb-3 mb-4">
                <h3 className="text-sm font-bold uppercase tracking-wider text-rose-400 flex items-center gap-2">
                  <Globe className="h-4 w-4" /> Save Target Website
                </h3>
                <button
                  type="button"
                  onClick={() => setShowWebsiteModal(false)}
                  className="p-1 rounded text-slate-400 hover:text-rose-400 hover:bg-slate-800 transition-all"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="flex flex-col gap-4 text-xs font-mono">
                <div>
                  <label className="block text-slate-400 mb-1 uppercase text-[10px]">Website Name</label>
                  <input
                    type="text"
                    value={newWebsite.name}
                    onChange={(e) => setNewWebsite((prev) => ({ ...prev, name: e.target.value }))}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-slate-200 focus:outline-none focus:border-rose-500/50"
                    placeholder="e.g. Wikipedia AI"
                  />
                </div>
                <div>
                  <label className="block text-slate-400 mb-1 uppercase text-[10px]">Target URL</label>
                  <input
                    type="text"
                    value={newWebsite.url}
                    onChange={(e) => setNewWebsite((prev) => ({ ...prev, url: e.target.value }))}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-slate-200 focus:outline-none focus:border-rose-500/50"
                    placeholder="https://en.wikipedia.org/wiki/..."
                  />
                </div>
                <div>
                  <label className="block text-slate-400 mb-1 uppercase text-[10px]">Description</label>
                  <textarea
                    value={newWebsite.description}
                    onChange={(e) => setNewWebsite((prev) => ({ ...prev, description: e.target.value }))}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-slate-200 focus:outline-none focus:border-rose-500/50 h-20 resize-none"
                    placeholder="Describe the website target..."
                  />
                </div>
                <div className="flex gap-2 mt-2">
                  <button
                    type="button"
                    onClick={handleSaveWebsite}
                    className="flex-1 py-2 px-4 rounded-lg bg-rose-500 text-white font-bold hover:bg-rose-600 shadow-[0_0_15px_rgba(244,63,94,0.3)] transition-all uppercase tracking-wider text-[11px]"
                  >
                    Save Website
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowWebsiteModal(false)}
                    className="py-2 px-4 rounded-lg bg-slate-950 border border-slate-800 text-slate-400 hover:border-slate-700 transition-all uppercase tracking-wider text-[11px]"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Create Behavior Pack (Persona) Modal */}
        {showPersonaModal && (
          <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center z-50 p-4">
            <div className="bg-slate-900 border border-purple-500/40 rounded-xl p-6 max-w-md w-full shadow-[0_0_30px_rgba(168,85,247,0.2)]">
              <div className="flex justify-between items-center border-b border-slate-800 pb-3 mb-4">
                <h3 className="text-sm font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
                  <Activity className="h-4 w-4" /> Create Custom Behavior Pack
                </h3>
                <button
                  type="button"
                  onClick={() => setShowPersonaModal(false)}
                  className="p-1 rounded text-slate-400 hover:text-purple-400 hover:bg-slate-800 transition-all"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="flex flex-col gap-4 text-xs font-mono">
                <div>
                  <label className="block text-slate-400 mb-1 uppercase text-[10px]">Behavior Pack Name</label>
                  <input
                    type="text"
                    value={newPersona.name}
                    onChange={(e) => setNewPersona((prev) => ({ ...prev, name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_') }))}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-slate-200 focus:outline-none focus:border-purple-500/50"
                    placeholder="e.g. aggressive_scraper"
                  />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-slate-400 mb-1 uppercase text-[10px]">Patience Level</label>
                    <select
                      value={newPersona.patience}
                      onChange={(e) => setNewPersona((prev) => ({ ...prev, patience: e.target.value }))}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-2.5 text-slate-200 focus:outline-none focus:border-purple-500/50"
                    >
                      <option value="low">Low</option>
                      <option value="medium">Medium</option>
                      <option value="high">High</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-slate-400 mb-1 uppercase text-[10px]">Interaction Style</label>
                    <select
                      value={newPersona.interaction_style}
                      onChange={(e) => setNewPersona((prev) => ({ ...prev, interaction_style: e.target.value }))}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-2.5 text-slate-200 focus:outline-none focus:border-purple-500/50"
                    >
                      <option value="low_touch">Low Touch</option>
                      <option value="inspect_then_move">Inspect Then Move</option>
                      <option value="minimal">Minimal</option>
                      <option value="confirm_before_click">Confirm Before Click</option>
                      <option value="touch_first">Touch First</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label className="block text-slate-400 mb-1 uppercase text-[10px]">Scroll Style</label>
                  <select
                    value={newPersona.scroll_style}
                    onChange={(e) => setNewPersona((prev) => ({ ...prev, scroll_style: e.target.value }))}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-2.5 text-slate-200 focus:outline-none focus:border-purple-500/50"
                  >
                    <option value="direct_scan">Direct Scan</option>
                    <option value="pause_and_read">Pause and Read</option>
                    <option value="long_skim">Long Skim</option>
                    <option value="short_partial_scrolls">Short Partial Scrolls</option>
                    <option value="section_scan">Section Scan</option>
                    <option value="short_swipes">Short Swipes</option>
                  </select>
                </div>
                <div>
                  <div className="flex justify-between text-slate-400 mb-1">
                    <span className="uppercase text-[10px]">Dwell Multiplier</span>
                    <span className="text-purple-400 font-bold">{newPersona.dwell_multiplier}x</span>
                  </div>
                  <input
                    type="range"
                    min="0.5"
                    max="3"
                    step="0.05"
                    value={newPersona.dwell_multiplier}
                    onChange={(e) => setNewPersona((prev) => ({ ...prev, dwell_multiplier: parseFloat(e.target.value) }))}
                    className="w-full accent-purple-500 bg-slate-950 h-1 rounded-lg appearance-none cursor-pointer"
                  />
                </div>
                <div>
                  <div className="flex justify-between text-slate-400 mb-1">
                    <span className="uppercase text-[10px]">Scroll Multiplier</span>
                    <span className="text-purple-400 font-bold">{newPersona.scroll_multiplier}x</span>
                  </div>
                  <input
                    type="range"
                    min="0.5"
                    max="3"
                    step="0.05"
                    value={newPersona.scroll_multiplier}
                    onChange={(e) => setNewPersona((prev) => ({ ...prev, scroll_multiplier: parseFloat(e.target.value) }))}
                    className="w-full accent-purple-500 bg-slate-950 h-1 rounded-lg appearance-none cursor-pointer"
                  />
                </div>
                <div className="flex gap-2 mt-2">
                  <button
                    type="button"
                    onClick={handleSavePersona}
                    className="flex-1 py-2 px-4 rounded-lg bg-purple-500 text-white font-bold hover:bg-purple-600 shadow-[0_0_15px_rgba(168,85,247,0.3)] transition-all uppercase tracking-wider text-[11px]"
                  >
                    Create Behavior Pack
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowPersonaModal(false)}
                    className="py-2 px-4 rounded-lg bg-slate-950 border border-slate-800 text-slate-400 hover:border-slate-700 transition-all uppercase tracking-wider text-[11px]"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

interface LoomPlayerProps {
  movieUrl: string;
  movieFilename: string;
  sessionRecording?: {
    run_id: string;
    enabled: boolean;
    event_count: number;
    events: Array<{
      index: number;
      action: string;
      status: string;
      label: string;
      elapsed_ms: number;
      metadata: any;
    }>;
  } | null;
}

function LoomPlayer({ movieUrl, movieFilename, sessionRecording }: LoomPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const eventListRef = useRef<HTMLDivElement>(null);

  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [isMuted, setIsMuted] = useState(false);
  const [hoveredEvent, setHoveredEvent] = useState<any | null>(null);
  const [hoveredX, setHoveredX] = useState(0);
  const [activeEventIndex, setActiveEventIndex] = useState(-1);

  const events = sessionRecording?.events || [];

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const handlePlay = () => setIsPlaying(true);
    const handlePause = () => setIsPlaying(false);
    const handleTimeUpdate = () => {
      setCurrentTime(video.currentTime);
    };
    const handleDurationChange = () => {
      setDuration(video.duration);
    };

    video.addEventListener('play', handlePlay);
    video.addEventListener('pause', handlePause);
    video.addEventListener('timeupdate', handleTimeUpdate);
    video.addEventListener('durationchange', handleDurationChange);

    if (video.duration) {
      setDuration(video.duration);
    }

    return () => {
      video.removeEventListener('play', handlePlay);
      video.removeEventListener('pause', handlePause);
      video.removeEventListener('timeupdate', handleTimeUpdate);
      video.removeEventListener('durationchange', handleDurationChange);
    };
  }, [movieUrl]);

  useEffect(() => {
    if (events.length === 0) return;
    const activeIdx = events.reduce((acc, ev, idx) => {
      const evTimeSec = ev.elapsed_ms / 1000;
      return evTimeSec <= currentTime ? idx : acc;
    }, -1);

    setActiveEventIndex(activeIdx);

    if (activeIdx !== -1 && eventListRef.current) {
      const activeEl = eventListRef.current.querySelector(`[data-event-index="${activeIdx}"]`);
      if (activeEl) {
        activeEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }
  }, [currentTime, events]);

  const togglePlay = () => {
    const video = videoRef.current;
    if (!video) return;
    if (isPlaying) {
      video.pause();
    } else {
      video.play().catch(() => {});
    }
  };

  const toggleMute = () => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !isMuted;
    setIsMuted(!isMuted);
  };

  const handleVolumeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const video = videoRef.current;
    if (!video) return;
    const val = parseFloat(e.target.value);
    video.volume = val;
    setVolume(val);
    if (val > 0 && isMuted) {
      video.muted = false;
      setIsMuted(false);
    }
  };

  const handleTimelineClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const timeline = timelineRef.current;
    const video = videoRef.current;
    if (!timeline || !video || duration === 0) return;

    const rect = timeline.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const percentage = Math.max(0, Math.min(1, clickX / rect.width));
    video.currentTime = percentage * duration;
  };

  const seekToEvent = (elapsedMs: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = elapsedMs / 1000;
    if (!isPlaying) {
      video.play().catch(() => {});
    }
  };

  const formatTime = (timeSec: number) => {
    if (isNaN(timeSec)) return '00:00';
    const mins = Math.floor(timeSec / 60);
    const secs = Math.floor(timeSec % 60);
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const getActionIcon = (action: string) => {
    const act = action.toLowerCase();
    if (act.includes('click')) return <MousePointer className="h-3.5 w-3.5 text-rose-400" />;
    if (act.includes('scroll')) return <Compass className="h-3.5 w-3.5 text-purple-400" />;
    if (act.includes('hover')) return <Eye className="h-3.5 w-3.5 text-amber-400" />;
    if (act.includes('type') || act.includes('key')) return <Sparkles className="h-3.5 w-3.5 text-emerald-400" />;
    if (act.includes('goto') || act.includes('navigate')) return <ExternalLink className="h-3.5 w-3.5 text-cyan-400" />;
    return <Info className="h-3.5 w-3.5 text-slate-400" />;
  };

  const getActionColorClass = (action: string) => {
    const act = action.toLowerCase();
    if (act.includes('click')) return 'bg-rose-500 border-rose-400 shadow-[0_0_8px_#f43f5e]';
    if (act.includes('scroll')) return 'bg-purple-500 border-purple-400 shadow-[0_0_8px_#a855f7]';
    if (act.includes('hover')) return 'bg-amber-500 border-amber-400 shadow-[0_0_8px_#f59e0b]';
    if (act.includes('type') || act.includes('key')) return 'bg-emerald-500 border-emerald-400 shadow-[0_0_8px_#10b981]';
    if (act.includes('goto') || act.includes('navigate')) return 'bg-cyan-500 border-cyan-400 shadow-[0_0_8px_#06b6d4]';
    return 'bg-slate-500 border-slate-400 shadow-[0_0_8px_#64748b]';
  };

  const toggleFullscreen = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.requestFullscreen) {
      video.requestFullscreen();
    }
  };

  return (
    <div className="w-full flex flex-col gap-4">
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
        <div className="xl:col-span-8 flex flex-col bg-slate-950 rounded-xl border border-slate-800 overflow-hidden relative group">
          <div className="relative aspect-video bg-black flex items-center justify-center">
            <video
              ref={videoRef}
              src={movieUrl}
              autoPlay
              className="w-full h-full object-contain"
              onClick={togglePlay}
            />
            
            {!isPlaying && (
              <button
                type="button"
                onClick={togglePlay}
                className="absolute inset-0 m-auto h-16 w-16 rounded-full bg-rose-500/90 hover:bg-rose-600 flex items-center justify-center text-white shadow-[0_0_25px_rgba(244,63,94,0.6)] transition-all transform hover:scale-105"
              >
                <Play className="h-8 w-8 fill-current ml-1" />
              </button>
            )}
          </div>

          <div className="bg-slate-900/95 border-t border-slate-800/80 p-3 flex flex-col gap-3">
            <div className="relative pt-1">
              {hoveredEvent && duration > 0 && (
                <div
                  className="absolute bottom-full mb-2 bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-[11px] font-mono text-slate-200 shadow-xl z-30 pointer-events-none w-56 -translate-x-1/2"
                  style={{ left: `${hoveredX}px` }}
                >
                  <div className="flex justify-between items-center border-b border-slate-800 pb-1 mb-1">
                    <span className="text-cyan-400 font-bold">
                      {formatTime(hoveredEvent.elapsed_ms / 1000)}
                    </span>
                    <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-slate-400">
                      {hoveredEvent.action}
                    </span>
                  </div>
                  <p className="text-slate-300 font-bold truncate">{hoveredEvent.label}</p>
                  {hoveredEvent.metadata && (
                    <div className="text-[10px] text-slate-500 mt-1 truncate">
                      {hoveredEvent.metadata.selector && `Selector: ${hoveredEvent.metadata.selector}`}
                      {hoveredEvent.metadata.wait_ms && `Wait: ${hoveredEvent.metadata.wait_ms}ms`}
                      {hoveredEvent.metadata.delta_y && `Scroll: ${hoveredEvent.metadata.delta_y}px`}
                    </div>
                  )}
                </div>
              )}

              <div
                ref={timelineRef}
                onClick={handleTimelineClick}
                className="h-2 w-full bg-slate-800 rounded-full cursor-pointer relative group/timeline"
              >
                <div
                  className="h-full bg-gradient-to-r from-rose-500 to-cyan-500 rounded-full absolute left-0 top-0"
                  style={{ width: `${duration > 0 ? (currentTime / duration) * 100 : 0}%` }}
                />

                {duration > 0 &&
                  events.map((ev, idx) => {
                    const posPct = ((ev.elapsed_ms / 1000) / duration) * 100;
                    if (posPct > 100) return null;
                    const isActive = idx === activeEventIndex;
                    return (
                      <button
                        key={idx}
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          seekToEvent(ev.elapsed_ms);
                        }}
                        onMouseEnter={(e) => {
                          const rect = timelineRef.current?.getBoundingClientRect();
                          if (rect) {
                            setHoveredEvent(ev);
                            setHoveredX(e.clientX - rect.left);
                          }
                        }}
                        onMouseLeave={() => setHoveredEvent(null)}
                        className={`absolute top-1/2 -translate-y-1/2 h-3 w-3 rounded-full border border-slate-950 transition-all transform hover:scale-150 z-10 ${getActionColorClass(
                          ev.action
                        )} ${isActive ? 'scale-125 ring-2 ring-white' : ''}`}
                        style={{ left: `${posPct}%`, transform: `translate(-50%, -50%)` }}
                      />
                    );
                  })}
              </div>
            </div>

            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={togglePlay}
                  className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-300 hover:text-rose-400 transition-all"
                >
                  {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 fill-current" />}
                </button>

                <div className="flex items-center gap-1.5 text-xs font-mono text-slate-400">
                  <Clock className="h-3.5 w-3.5 text-slate-500" />
                  <span>{formatTime(currentTime)}</span>
                  <span className="text-slate-600">/</span>
                  <span>{formatTime(duration)}</span>
                </div>

                <span className="text-[10px] text-slate-500 font-mono hidden md:inline truncate max-w-[180px]" title={movieFilename}>
                  {movieFilename}
                </span>
              </div>

              <div className="flex items-center gap-4">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={toggleMute}
                    className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-300 hover:text-cyan-400 transition-all"
                  >
                    {isMuted || volume === 0 ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
                  </button>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={isMuted ? 0 : volume}
                    onChange={handleVolumeChange}
                    className="w-16 accent-cyan-500 bg-slate-800 h-1 rounded-lg appearance-none cursor-pointer"
                  />
                </div>

                <button
                  type="button"
                  onClick={toggleFullscreen}
                  className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-300 hover:text-cyan-400 transition-all"
                  title="Fullscreen"
                >
                  <Maximize className="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
        </div>

        <div className="xl:col-span-4 flex flex-col bg-slate-900/60 rounded-xl border border-slate-800 overflow-hidden h-[400px] xl:h-[450px]">
          <div className="bg-slate-900 border-b border-slate-800/80 p-3 flex justify-between items-center">
            <span className="text-xs font-bold uppercase tracking-wider text-purple-400 flex items-center gap-1.5">
              <Activity className="h-4 w-4" /> Interactive Event List
            </span>
            <span className="text-[10px] font-mono text-slate-500 bg-slate-950 border border-slate-800 px-2 py-0.5 rounded">
              {events.length} Events
            </span>
          </div>

          {events.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center text-slate-500 p-4 text-center">
              <Clock className="h-8 w-8 text-slate-700 mb-2 animate-pulse" />
              <p className="text-xs font-bold">No timeline events stamped</p>
              <p className="text-[10px] text-slate-600 mt-1">Timeline events appear after a successful simulation session.</p>
            </div>
          ) : (
            <div
              ref={eventListRef}
              className="flex-1 overflow-y-auto p-2 flex flex-col gap-1.5 scrollbar-thin scrollbar-thumb-slate-800"
            >
              {events.map((ev, idx) => {
                const isActive = idx === activeEventIndex;
                return (
                  <button
                    key={idx}
                    type="button"
                    data-event-index={idx}
                    onClick={() => seekToEvent(ev.elapsed_ms)}
                    className={`w-full text-left p-2.5 rounded-lg border font-mono text-xs transition-all flex items-start gap-3 focus:outline-none ${
                      isActive
                        ? 'bg-gradient-to-r from-purple-950/40 to-slate-950 border-purple-500 text-purple-200 shadow-[0_0_12px_rgba(168,85,247,0.15)]'
                        : 'bg-slate-950/40 border-slate-900 text-slate-400 hover:bg-slate-950/80 hover:border-slate-800'
                    }`}
                  >
                    <div className="mt-0.5 shrink-0">{getActionIcon(ev.action)}</div>
                    <div className="flex-1 min-w-0">
                      <div className="flex justify-between items-center mb-1">
                        <span className={`text-[10px] font-bold uppercase tracking-wide ${isActive ? 'text-purple-400' : 'text-slate-500'}`}>
                          {ev.action}
                        </span>
                        <span className="text-[10px] text-slate-500 font-bold">
                          {formatTime(ev.elapsed_ms / 1000)}
                        </span>
                      </div>
                      <p className={`font-bold truncate ${isActive ? 'text-white' : 'text-slate-300'}`}>
                        {ev.label}
                      </p>
                      {ev.metadata && (
                        <div className="text-[10px] text-slate-500 mt-1 flex flex-wrap gap-x-2 gap-y-0.5 border-t border-slate-900 pt-1">
                          {ev.metadata.selector && (
                            <span className="truncate max-w-[180px]">sel: {ev.metadata.selector}</span>
                          )}
                          {ev.metadata.wait_ms && <span>wait: {ev.metadata.wait_ms}ms</span>}
                          {ev.metadata.delta_y && <span>scroll: {ev.metadata.delta_y}px</span>}
                        </div>
                      )}
                    </div>
                    <ChevronRight className={`h-4 w-4 shrink-0 mt-1.5 transition-transform ${isActive ? 'text-purple-400 translate-x-0.5' : 'text-slate-700'}`} />
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
