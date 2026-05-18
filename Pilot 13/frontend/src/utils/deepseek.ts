export const DEEPSEEK_API_KEY_STORAGE_KEY = 'gasum-deepseek-api-key';

export function getDeepSeekApiKey(): string {
  if (typeof window === 'undefined') return '';
  return localStorage.getItem(DEEPSEEK_API_KEY_STORAGE_KEY)?.trim() ?? '';
}

export function setDeepSeekApiKey(apiKey: string): void {
  if (typeof window === 'undefined') return;
  const trimmed = apiKey.trim();
  if (trimmed) {
    localStorage.setItem(DEEPSEEK_API_KEY_STORAGE_KEY, trimmed);
  } else {
    localStorage.removeItem(DEEPSEEK_API_KEY_STORAGE_KEY);
  }
}
