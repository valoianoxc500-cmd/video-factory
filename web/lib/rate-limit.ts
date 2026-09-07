/**
 * Per-user rate limiting for expensive actions.
 *
 * In-memory and per-instance, which is the honest limit of this: serverless
 * spreads requests across instances, so it throttles a burst from one client
 * rather than enforcing a global quota. It is a guard against accidental
 * hammering, not a billing control -- real quotas belong in the database
 * alongside credits, which `usage_events` exists to support.
 *
 * Generation is already serialised elsewhere (one active job per user), so
 * this mainly stops a stuck client from filling the queue.
 */

interface Bucket {
  count: number;
  resetAt: number;
}

const buckets = new Map<string, Bucket>();

export interface RateLimitResult {
  allowed: boolean;
  retryAfterSeconds: number;
}

export function rateLimit(
  key: string,
  { limit, windowSeconds }: { limit: number; windowSeconds: number },
): RateLimitResult {
  const now = Date.now();
  const existing = buckets.get(key);

  if (!existing || now >= existing.resetAt) {
    buckets.set(key, { count: 1, resetAt: now + windowSeconds * 1000 });
    return { allowed: true, retryAfterSeconds: 0 };
  }

  if (existing.count >= limit) {
    return {
      allowed: false,
      retryAfterSeconds: Math.max(1, Math.ceil((existing.resetAt - now) / 1000)),
    };
  }

  existing.count += 1;
  // Bound the map so a long-lived instance cannot grow without limit.
  if (buckets.size > 5000) {
    for (const [k, v] of buckets) {
      if (now >= v.resetAt) buckets.delete(k);
    }
  }
  return { allowed: true, retryAfterSeconds: 0 };
}
