import { useState, useEffect } from 'react';
import {
  Play,
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

  // Load personas on mount
  useEffect(() => {
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
    fetchPersonas();
  }, []);

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
            <span className="text-xs font-mono text-emerald-400 tracking-wider">STANDALONE DEPLOYMENT // PORT 5180</span>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          {/* Left Column: Config Panel */}
          <div className="lg:col-span-5 flex flex-col gap-6">
            {/* Target Website Card */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
              <h2 className="text-sm font-bold uppercase tracking-wider text-rose-400 mb-4 flex items-center gap-2">
                <Globe className="h-4 w-4" /> 1. Model Target Website
              </h2>
              <div className="flex flex-col gap-4">
                <div>
                  <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Simulation Provider</label>
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setProvider('generic');
                        setUrl('https://en.wikipedia.org/wiki/Artificial_intelligence');
                      }}
                      className={`py-2 px-3 rounded-lg border text-xs font-bold transition-all ${
                        provider === 'generic'
                          ? 'bg-rose-500/10 border-rose-500 text-rose-400 shadow-[0_0_10px_rgba(244,63,94,0.2)]'
                          : 'bg-slate-950 border-slate-800 text-slate-400 hover:border-slate-700'
                      }`}
                    >
                      Generic Web (Wiki)
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setProvider('linkedin');
                        setUrl('https://www.linkedin.com/feed');
                      }}
                      className={`py-2 px-3 rounded-lg border text-xs font-bold transition-all ${
                        provider === 'linkedin'
                          ? 'bg-rose-500/10 border-rose-500 text-rose-400 shadow-[0_0_10px_rgba(244,63,94,0.2)]'
                          : 'bg-slate-950 border-slate-800 text-slate-400 hover:border-slate-700'
                      }`}
                    >
                      LinkedIn Feed
                    </button>
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Target URL</label>
                  <input
                    type="text"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-xs font-mono text-slate-200 focus:outline-none focus:border-rose-500/50"
                    placeholder="https://example.com"
                  />
                </div>

                <div>
                  <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Custom Mission Prompt (Optional)</label>
                  <textarea
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-3 text-xs font-mono text-slate-200 focus:outline-none focus:border-rose-500/50 h-16 resize-none"
                    placeholder="e.g. skim the feed like a late-day review, hover interesting posts..."
                  />
                </div>
              </div>
            </div>

            {/* Behavior Pack Card */}
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 backdrop-blur-md shadow-[0_4px_20px_rgba(0,0,0,0.3)]">
              <h2 className="text-sm font-bold uppercase tracking-wider text-purple-400 mb-4 flex items-center gap-2">
                <Activity className="h-4 w-4" /> 2. Assign Behavior Pack
              </h2>
              <div className="flex flex-col gap-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-mono text-slate-400 mb-1.5 uppercase">Persona Profile</label>
                    <select
                      value={persona}
                      onChange={(e) => setPersona(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg py-2 px-2.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500/50"
                    >
                      {personas.length > 0 ? (
                        personas.map((p) => (
                          <option key={p.name} value={p.name}>
                            {p.name.replace(/_/g, ' ')}
                          </option>
                        ))
                      ) : (
                        <>
                          <option value="curious_reader">Curious Reader</option>
                          <option value="focused_power_user">Focused Power User</option>
                          <option value="impatient_scanner">Impatient Scanner</option>
                          <option value="slow_reader">Slow Reader</option>
                          <option value="methodical_operator">Methodical Operator</option>
                        </>
                      )}
                    </select>
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

                <div>
                  <label className="block text-xs font-mono text-slate-400 mb-2 uppercase">Active Engagement Techniques</label>
                  <div className="grid grid-cols-2 gap-2">
                    {Object.keys(variations).map((key) => {
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

              <div className="flex-1 flex flex-col items-center justify-center border border-slate-800/80 bg-slate-950 rounded-lg p-4 relative overflow-hidden">
                {loading ? (
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
                ) : result?.movie_url ? (
                  <div className="w-full h-full flex flex-col gap-4">
                    <video
                      src={result.movie_url}
                      controls
                      autoPlay
                      className="w-full rounded-lg border border-cyan-500/40 shadow-[0_0_20px_rgba(6,182,212,0.15)] bg-black"
                    />
                    <div className="flex items-center justify-between text-xs font-mono text-slate-400 px-1">
                      <span className="flex items-center gap-1">
                        <Video className="h-3.5 w-3.5 text-cyan-400" /> Recorded: {result.movie_filename}
                      </span>
                      <span className="text-slate-500">Saved to ~/Desktop/tokyo-latest-watch-movie.webm</span>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col items-center gap-3 text-slate-500 text-center max-w-sm">
                    <Video className="h-12 w-12 text-slate-700 animate-pulse" />
                    <p className="text-sm font-bold">No Active Simulation</p>
                    <p className="text-xs">Configure your techniques and click "Launch Imposter5 Session" above. A headed browser will pop up and record the visual mechanics with the red HUMAN MOUSE cursor.</p>
                  </div>
                )}
              </div>
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
      </div>
    </div>
  );
}
