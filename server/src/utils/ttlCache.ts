export class TTLCache<T> {
  private readonly cache = new Map<string, { expiresAt: number; value: T }>();

  constructor(private readonly ttlMs: number) {}

  get(key: string): T | null {
    const entry = this.cache.get(key);
    if (!entry) {
      return null;
    }

    if (Date.now() > entry.expiresAt) {
      this.cache.delete(key);
      return null;
    }

    return entry.value;
  }

  set(key: string, value: T): T {
    this.cache.set(key, {
      value,
      expiresAt: Date.now() + this.ttlMs
    });

    return value;
  }

  clear(): void {
    this.cache.clear();
  }
}
