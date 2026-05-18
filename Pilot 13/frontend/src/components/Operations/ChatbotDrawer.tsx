import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import type { Recommendation, Site } from '../../types';
import { getRouteDisplayTimeHours } from '../../utils/routeMetrics';
import { getDeepSeekApiKey, setDeepSeekApiKey } from '../../utils/deepseek';

// ─── Types ───────────────────────────────────────────────────────────────────

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

interface ChatbotDrawerProps {
  recommendation: Recommendation;
  sites: Site[];
  onClose: () => void;
}

// ─── Build system prompt from recommendation data ─────────────────────────────

function buildSystemPrompt(recommendation: Recommendation, sites: Site[]): string {
  const siteMap = Object.fromEntries(sites.map((s) => [s.id, s]));

  const routeSummaries = recommendation.routes.map((r) => {
    const stops = r.stops.map((s) => {
      const site = siteMap[s.site_id];
      const op = s.swap_operation;
      const dropped = op?.containers_dropped?.length ?? 0;
      const picked = op?.containers_picked?.length ?? 0;
      const action =
        dropped > 0 && picked > 0
          ? `drop ${dropped} / pick ${picked}`
          : dropped > 0
          ? `drop ${dropped}`
          : picked > 0
          ? `pick ${picked}`
          : 'transit';
      return `    • ${site?.name ?? s.site_id} (${action})`;
    });
    return `  Truck ${r.truck_id} – Day ${r.day_index + 1}: ${r.total_distance_km.toFixed(0)} km, ${getRouteDisplayTimeHours(r).toFixed(1)}h\n${stops.join('\n')}`;
  });

  const siteRisks = sites
    .filter((s) => s.risk_level !== 'normal')
    .map((s) => `  • ${s.name} [${s.risk_level}]: ${s.hours_to_critical?.toFixed(1) ?? 'N/A'}h to critical`)
    .join('\n');

  return `You are a logistics expert assistant for GASUM Biogas, helping operators understand AI-generated route recommendations.

## Current Recommendation Summary
- Objective: ${(recommendation as { objective?: string }).objective ?? 'balanced'}
- Total routes: ${recommendation.routes.length}
- Sites served: ${recommendation.sites_served}
- Total distance: ${recommendation.total_distance_km.toFixed(0)} km
- Total cost: ${recommendation.total_cost_eur.toFixed(0)} EUR
- Energy moved: ${recommendation.total_mwh_moved?.toFixed(1) ?? 'N/A'} MWh
- EUR/MWh: ${recommendation.eur_per_mwh?.toFixed(2) ?? 'N/A'}
- Feasibility: ${recommendation.feasibility_level ?? 'STRICT'}
- Explanation: ${recommendation.explanation ?? ''}

## Route Details
${routeSummaries.join('\n\n')}

## Sites at Risk
${siteRisks || '  None'}

## Warnings
${recommendation.warnings?.map((w) => `  • ${w}`).join('\n') || '  None'}

Answer questions clearly and concisely. If asked why certain decisions were made, reason based on the data above. Speak in plain language suitable for logistics operators.`;
}

// ─── Chat message bubble ──────────────────────────────────────────────────────

const MessageBubble: React.FC<{ msg: Message }> = ({ msg }) => {
  const isUser = msg.role === 'user';
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-3`}>
      {!isUser && (
        <div className="w-6 h-6 rounded-full bg-violet-600 flex items-center justify-center text-[10px] font-bold mr-2 mt-0.5 flex-shrink-0">
          AI
        </div>
      )}
      <div
        className={`max-w-[85%] px-3 py-2 rounded-xl text-sm leading-relaxed whitespace-pre-wrap ${
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

export const ChatbotDrawer: React.FC<ChatbotDrawerProps> = ({ recommendation, sites, onClose }) => {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content: `Hi! I'm your logistics AI assistant powered by DeepSeek. I have full context on this route plan (${recommendation.routes.length} routes, ${recommendation.sites_served} sites, ${recommendation.total_cost_eur.toFixed(0)} EUR). Ask me anything about why this plan was generated.`,
    },
  ]);
  const [input, setInput] = useState('');
  const [apiKey, setApiKey] = useState(() => getDeepSeekApiKey());
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

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

    const newMessages: Message[] = [...messages, { role: 'user', content: text }];
    setMessages(newMessages);
    setInput('');
    setLoading(true);
    setError(null);

    try {
      const systemPrompt = buildSystemPrompt(recommendation, sites);
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

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return createPortal(
    <div className="fixed inset-0" style={{ zIndex: 9998 }}>
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="absolute top-0 right-0 h-full w-[400px] max-w-[95vw] bg-slate-800 border-l border-slate-600 shadow-2xl flex flex-col">

        {/* Header */}
        <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-slate-700">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-full bg-violet-600 flex items-center justify-center">
              <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
              </svg>
            </div>
            <h3 className="text-sm font-semibold text-white">AI Plan Assistant</h3>
            <span className="text-[10px] text-blue-400 bg-blue-900/40 px-1.5 py-0.5 rounded font-medium">DeepSeek</span>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none px-1">&times;</button>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4">
          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}

          {loading && (
            <div className="flex justify-start mb-3">
              <div className="w-6 h-6 rounded-full bg-violet-600 flex items-center justify-center text-[10px] font-bold mr-2 mt-0.5 flex-shrink-0">
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
              <button
                onClick={() => setError(null)}
                className="ml-2 text-red-400 hover:text-red-200"
              >
                dismiss
              </button>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="flex-shrink-0 p-3 border-t border-slate-700">
          <div className="mb-2">
            <div className="flex items-center gap-2 mb-1">
              <label htmlFor="deepseek-api-key-plan" className="text-[11px] text-slate-300 font-medium">
                DeepSeek API key
              </label>
              <button
                type="button"
                onClick={() => setDeepSeekApiKey(apiKey)}
                className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
              >
                Save
              </button>
            </div>
            <input
              id="deepseek-api-key-plan"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-... or leave empty to use server key"
              className="w-full px-3 py-2 bg-slate-700 border border-slate-600 rounded-lg text-xs text-white placeholder-slate-400 focus:outline-none focus:border-violet-500"
            />
          </div>
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about this plan… (Enter to send, Shift+Enter for newline)"
              rows={2}
              className="flex-1 px-3 py-2 bg-slate-700 border border-slate-600 rounded-lg text-sm text-white placeholder-slate-400 focus:outline-none focus:border-violet-500 resize-none"
            />
            <button
              onClick={sendMessage}
              disabled={!input.trim() || loading}
              className="px-3 py-2 bg-violet-600 hover:bg-violet-700 disabled:bg-slate-700 disabled:text-slate-500 rounded-lg transition-colors flex-shrink-0"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            </button>
          </div>
          <p className="text-[10px] text-slate-600 mt-1.5">Model: deepseek-chat · Uses saved DeepSeek key when provided, otherwise server fallback</p>
        </div>
      </div>
    </div>,
    document.body,
  );
};
