import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import type { Site } from '../../types';
import { getDeepSeekApiKey, setDeepSeekApiKey } from '../../utils/deepseek';

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

interface SituationDrawerProps {
  sites: Site[];
  onClose: () => void;
}

// ─── Build system prompt from all sites ───────────────────────────────────────

function buildSystemPrompt(sites: Site[]): string {
  const critical = sites.filter((s) => s.risk_level === 'critical');
  const warning  = sites.filter((s) => s.risk_level === 'warning');
  const normal   = sites.filter((s) => s.risk_level === 'normal' || s.risk_level === 'safe');
  const producers = sites.filter((s) => s.site_type === 'production');
  const consumers = sites.filter((s) => s.site_type !== 'production');

  const formatHTC = (h: number) => {
    if (!h || h >= 99999) return 'N/A';
    if (h < 1) return `${Math.round(h * 60)}min`;
    if (h < 24) return `${h.toFixed(1)}h`;
    return `${Math.round(h / 24)}d`;
  };

  const siteLines = sites
    .sort((a, b) => (b.risk_score ?? 0) - (a.risk_score ?? 0))
    .map((s) => {
      const htc = formatHTC(s.hours_to_critical ?? 0);
      const util = s.utilization_usable_pct?.toFixed(0) ?? '?';
      const mwh  = s.total_mwh_usable?.toFixed(1) ?? s.total_mwh?.toFixed(1) ?? '?';
      const bays = s.bays_fixed ?? s.bays?.length ?? 0;
      let extra = '';
      if (s.site_type === 'production' && s.production) {
        const rate = s.production.effective_kg_per_h ?? s.production.kg_per_h;
        extra = ` | production=${rate != null ? rate.toFixed(0) + ' kg/h' : 'N/A'} | flaring_risk=${s.flaring_loss_eur_per_h != null ? s.flaring_loss_eur_per_h.toFixed(0) + ' EUR/h' : 'none'}`;
      } else if (s.consumption_rate_kg_hour) {
        extra = ` | consumption=${s.consumption_rate_kg_hour.toFixed(0)} kg/h`;
      }
      return `  • [${s.risk_level.toUpperCase()}] ${s.name} (${s.site_type}) | util=${util}% | inventory=${mwh} MWh | bays=${bays} | htc=${htc}${extra}`;
    })
    .join('\n');

  return `You are a logistics expert assistant for GASUM Biogas operations in Finland.
Your job is to analyse the CURRENT NETWORK STATE and help operators understand risks, priorities, and actions.

## Network Overview
- Total sites: ${sites.length}
- Critical (immediate action needed): ${critical.length}
- Warning (action needed within 48h): ${warning.length}
- Normal/Safe: ${normal.length}
- Producers: ${producers.length}  |  Consumers: ${consumers.length}

## All Sites — sorted by risk score (highest first)
${siteLines}

## Critical Sites Details
${critical.length === 0 ? '  None' : critical.map((s) => {
  const htc = formatHTC(s.hours_to_critical ?? 0);
  return `  ⚠ ${s.name}: ${htc} until critical | ${s.total_mwh_usable?.toFixed(1) ?? '?'} MWh usable | ${s.bays?.length ?? 0} bays`;
}).join('\n')}

## Warning Sites Details
${warning.length === 0 ? '  None' : warning.map((s) => {
  const htc = formatHTC(s.hours_to_critical ?? 0);
  return `  ! ${s.name}: ${htc} until critical | ${s.total_mwh_usable?.toFixed(1) ?? '?'} MWh usable`;
}).join('\n')}

Answer in clear, plain language suitable for logistics operators.
For the situation report: lead with the most urgent risk, then summarise producer/consumer balance, then suggest the most important action.`;
}

// ─── Message bubble ───────────────────────────────────────────────────────────

const MessageBubble: React.FC<{ msg: Message }> = ({ msg }) => {
  const isUser = msg.role === 'user';
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-3`}>
      {!isUser && (
        <div className="w-6 h-6 rounded-full bg-emerald-600 flex items-center justify-center text-[10px] font-bold mr-2 mt-0.5 flex-shrink-0">
          AI
        </div>
      )}
      <div
        className={`max-w-[88%] px-3 py-2 rounded-xl text-sm leading-relaxed whitespace-pre-wrap ${
          isUser
            ? 'bg-blue-600 text-white rounded-br-none'
            : 'bg-slate-700 text-slate-100 rounded-bl-none'
        }`}
      >
        {msg.content}
      </div>
    </div>
  );
};

// ─── Main drawer ──────────────────────────────────────────────────────────────

export const SituationDrawer: React.FC<SituationDrawerProps> = ({ sites, onClose }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [apiKey, setApiKey] = useState(() => getDeepSeekApiKey());
  const [showApiKeyEditor, setShowApiKeyEditor] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  useEffect(() => {
    if (error) setError(null);
  }, [apiKey]);

  const saveApiKey = () => {
    setDeepSeekApiKey(apiKey);
    setShowApiKeyEditor(false);
    inputRef.current?.focus();
  };

  const fireMessage = async (text: string, history: Message[]) => {
    const newMessages: Message[] = [...history, { role: 'user' as const, content: text }];
    setMessages(newMessages);
    setLoading(true);
    setError(null);

    try {
      const systemPrompt = buildSystemPrompt(sites);
      const trimmedApiKey = apiKey.trim();
      setDeepSeekApiKey(trimmedApiKey);
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          system: systemPrompt,
          api_key: trimmedApiKey || undefined,
          messages: newMessages.map((m) => ({ role: m.role, content: m.content })),
        }),
      });

      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data?.detail || `Server error ${response.status}`);
      }

      const data = await response.json();
      setMessages([...newMessages, { role: 'assistant', content: data.content }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reach AI assistant');
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const sendMessage = () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput('');
    fireMessage(text, messages);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const critical = sites.filter((s) => s.risk_level === 'critical').length;
  const warning  = sites.filter((s) => s.risk_level === 'warning').length;

  return createPortal(
    <div className="fixed inset-0" style={{ zIndex: 9998 }}>
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="absolute top-0 right-0 h-full w-[420px] max-w-[95vw] bg-slate-800 border-l border-slate-600 shadow-2xl flex flex-col">

        {/* Header */}
        <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-slate-700">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-full bg-emerald-600 flex items-center justify-center">
              <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            </div>
            <h3 className="text-sm font-semibold text-white">Network Situation</h3>
            <span className="text-[10px] text-emerald-400 bg-emerald-900/40 px-1.5 py-0.5 rounded font-medium">AI</span>
            <button
              type="button"
              onClick={() => setShowApiKeyEditor((current) => !current)}
              className="ml-1 flex items-center gap-1 rounded-full border border-slate-600 bg-slate-700/70 px-2 py-1 text-[10px] font-medium text-slate-200 hover:bg-slate-700"
            >
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a5 5 0 00-9.9 1M6 11a2 2 0 00-2 2v4a2 2 0 002 2h10a2 2 0 002-2v-4a2 2 0 00-2-2H6z" />
              </svg>
              {apiKey.trim() ? 'API key saved' : 'API key'}
            </button>
          </div>
          {/* Live risk summary pills */}
          <div className="flex items-center gap-1.5">
            {critical > 0 && (
              <span className="text-[10px] bg-red-900/60 text-red-300 border border-red-700 px-1.5 py-0.5 rounded-full font-medium">
                {critical} critical
              </span>
            )}
            {warning > 0 && (
              <span className="text-[10px] bg-amber-900/60 text-amber-300 border border-amber-700 px-1.5 py-0.5 rounded-full font-medium">
                {warning} warning
              </span>
            )}
            <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none px-1 ml-1">&times;</button>
          </div>
        </div>

        {showApiKeyEditor && (
          <div className="flex-shrink-0 border-b border-slate-700/60 bg-slate-800 px-4 py-3">
            <div className="flex items-center gap-2 mb-2">
              <label htmlFor="deepseek-api-key-situation" className="text-[11px] text-slate-300 font-medium">
                DeepSeek API key
              </label>
              <span className="text-[10px] text-slate-500">
                Stored locally in this browser
              </span>
            </div>
            <div className="flex gap-2">
              <input
                id="deepseek-api-key-situation"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-... or leave empty to use server key"
                className="flex-1 px-3 py-2 bg-slate-700 border border-slate-600 rounded-lg text-xs text-white placeholder-slate-400 focus:outline-none focus:border-emerald-500"
              />
              <button
                type="button"
                onClick={saveApiKey}
                className="px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-xs font-medium text-white transition-colors"
              >
                Save
              </button>
            </div>
          </div>
        )}

        {/* Quick-ask chips */}
        <div className="flex-shrink-0 px-3 pt-2 pb-1 flex flex-wrap gap-1.5 border-b border-slate-700/50">
          {[
            'Which sites need delivery today?',
            'Producer overflow risk?',
            'Consumption trend summary',
            'Lowest inventory sites',
          ].map((chip) => (
            <button
              key={chip}
              onClick={() => { setInput(chip); inputRef.current?.focus(); }}
              className="text-[11px] px-2 py-0.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded-full transition-colors"
            >
              {chip}
            </button>
          ))}
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4">
          {messages.length === 0 && !loading && (
            <div className="flex items-center justify-center h-24 text-slate-500 text-sm">
              Pick a quick prompt or ask your own question about the network.
            </div>
          )}
          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}

          {loading && (
            <div className="flex justify-start mb-3">
              <div className="w-6 h-6 rounded-full bg-emerald-600 flex items-center justify-center text-[10px] font-bold mr-2 mt-0.5 flex-shrink-0">
                AI
              </div>
              <div className="bg-slate-700 rounded-xl rounded-bl-none px-3 py-2">
                <div className="flex gap-1 items-center h-5">
                  <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              </div>
            </div>
          )}

          {error && (
            <div className="mb-3 px-3 py-2 bg-red-900/40 border border-red-600 rounded-lg text-xs text-red-300">
              {error}
              <button onClick={() => setError(null)} className="ml-2 text-red-400 hover:text-red-200">dismiss</button>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="flex-shrink-0 p-3 border-t border-slate-700">
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about the network… (Enter to send)"
              rows={2}
              className="flex-1 px-3 py-2 bg-slate-700 border border-slate-600 rounded-lg text-sm text-white placeholder-slate-400 focus:outline-none focus:border-emerald-500 resize-none"
            />
            <button
              onClick={sendMessage}
              disabled={!input.trim() || loading}
              className="px-3 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-700 disabled:text-slate-500 rounded-lg transition-colors flex-shrink-0"
            >
              <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            </button>
          </div>
          <p className="text-[10px] text-slate-600 mt-1.5">Context: all {sites.length} sites · API key available from header or server fallback</p>
        </div>
      </div>
    </div>,
    document.body,
  );
};
