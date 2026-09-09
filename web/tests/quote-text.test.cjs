/**
 * Quote Studio's text gateway: what the customer sees vs what the log gets.
 *
 * These exist because a production failure ("Quote writing is temporarily
 * unavailable.") could not be diagnosed remotely: every distinct cause
 * produced the same sentence and wrote nothing anywhere. So the properties
 * pinned here are the boundary itself -- the customer sentence never carries
 * a provider name or a status code, and every failure path writes exactly one
 * server-side line that does.
 *
 * No network: `fetch` is stubbed per case.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { writeQuoteText, QuoteTextError } = require("../.test-build/quote-text.js");

/** Run `fn` with a stubbed fetch and env, capturing console.error. */
async function withGateway({ key = "test-key", fetchImpl }, fn) {
  const realFetch = global.fetch;
  const realKey = process.env.FAL_KEY;
  const realError = console.error;
  const logs = [];

  if (key === null) delete process.env.FAL_KEY;
  else process.env.FAL_KEY = key;
  global.fetch = fetchImpl ?? (async () => { throw new Error("no fetch expected"); });
  console.error = (...args) => logs.push(args.join(" "));

  try {
    return await fn(logs);
  } finally {
    global.fetch = realFetch;
    console.error = realError;
    if (realKey === undefined) delete process.env.FAL_KEY;
    else process.env.FAL_KEY = realKey;
  }
}

const ok = (body) => async () => ({
  ok: true,
  status: 200,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const notOk = (status, body) => async () => ({
  ok: false,
  status,
  json: async () => ({}),
  text: async () => body,
});

/** Anything that would leak the provider or the transport to a customer. */
const LEAKY = /fal|gemini|http|status|\b\d{3}\b|token|key|json|stack|api/i;

// ── the happy path still works ───────────────────────────────────────

test("returns the model's text unchanged", async () => {
  await withGateway(
    { fetchImpl: ok({ output: '["a","b"]', error: null }) },
    async () => {
      assert.equal(await writeQuoteText("p"), '["a","b"]');
    },
  );
});

test("a successful call logs nothing", async () => {
  await withGateway(
    { fetchImpl: ok({ output: "text", error: null }) },
    async (logs) => {
      await writeQuoteText("p");
      assert.deepEqual(logs, []);
    },
  );
});

// ── the production failure: missing credential ───────────────────────

test("a missing credential fails as 503 and names the variable in the log", async () => {
  await withGateway({ key: null }, async (logs) => {
    await assert.rejects(
      () => writeQuoteText("p"),
      (err) => {
        assert.ok(err instanceof QuoteTextError);
        assert.equal(err.status, 503);
        assert.equal(err.message, "Quote writing is temporarily unavailable.");
        return true;
      },
    );
    assert.equal(logs.length, 1, "a misconfiguration must be logged exactly once");
    assert.match(logs[0], /FAL_KEY/, "the log must say which variable to set");
    assert.match(logs[0], /not configured/);
  });
});

test("an empty credential is treated as missing, not as a valid key", async () => {
  await withGateway({ key: "" }, async (logs) => {
    await assert.rejects(() => writeQuoteText("p"), { status: 503 });
    assert.match(logs[0], /FAL_KEY/);
  });
});

test("the credential value is never written to the log", async () => {
  const secret = "id-abcdef:secret-0123456789";
  await withGateway(
    { key: secret, fetchImpl: notOk(401, "unauthorized") },
    async (logs) => {
      await assert.rejects(() => writeQuoteText("p"));
      for (const line of logs) {
        assert.ok(!line.includes(secret), "the key leaked into a log line");
        assert.ok(!line.includes("secret-0123456789"));
      }
    },
  );
});

// ── provider failures are distinguishable in the log only ────────────

test("a rejected credential logs the status and body, customer sees neither", async () => {
  await withGateway(
    { fetchImpl: notOk(401, '{"detail":"Cannot access application"}') },
    async (logs) => {
      await assert.rejects(
        () => writeQuoteText("p"),
        (err) => {
          assert.equal(err.status, 502);
          assert.equal(err.message, "Quote writing is temporarily unavailable.");
          assert.ok(!LEAKY.test(err.message), "customer message leaked internals");
          return true;
        },
      );
      assert.match(logs[0], /401/);
      assert.match(logs[0], /Cannot access application/);
    },
  );
});

test("a rate limit is reported as busy, not as unavailable", async () => {
  await withGateway({ fetchImpl: notOk(429, "slow down") }, async (logs) => {
    await assert.rejects(
      () => writeQuoteText("p"),
      (err) => {
        assert.equal(err.status, 429);
        assert.match(err.message, /busy/i);
        return true;
      },
    );
    assert.match(logs[0], /429/);
  });
});

test("a 200 carrying an error field is a failure, not a success", async () => {
  // This endpoint answers 200 with a populated `error` on a refusal.
  await withGateway(
    { fetchImpl: ok({ output: "", error: { message: "refused" } }) },
    async (logs) => {
      await assert.rejects(() => writeQuoteText("p"), { status: 502 });
      assert.match(logs[0], /error field/);
      assert.match(logs[0], /refused/);
    },
  );
});

test("an empty output is reported as such rather than returned", async () => {
  await withGateway({ fetchImpl: ok({ output: "   ", error: null }) }, async (logs) => {
    await assert.rejects(
      () => writeQuoteText("p"),
      (err) => {
        assert.equal(err.status, 502);
        assert.match(err.message, /no usable text/);
        return true;
      },
    );
    assert.match(logs[0], /empty output/);
  });
});

test("a transport fault is logged as unexpected, not as a provider outage", async () => {
  await withGateway(
    { fetchImpl: async () => { throw new TypeError("fetch failed"); } },
    async (logs) => {
      await assert.rejects(() => writeQuoteText("p"), { status: 502 });
      assert.match(logs[0], /unexpected failure/);
    },
  );
});

test("a timeout is reported as slow and logged with its budget", async () => {
  await withGateway(
    {
      fetchImpl: async () => {
        const err = new Error("aborted");
        err.name = "AbortError";
        throw err;
      },
    },
    async (logs) => {
      await assert.rejects(
        () => writeQuoteText("p"),
        (err) => {
          assert.equal(err.status, 504);
          assert.match(err.message, /too long/i);
          return true;
        },
      );
      assert.match(logs[0], /exceeded/);
    },
  );
});

// ── the customer boundary, across every path ─────────────────────────

test("no failure path ever shows a customer an internal detail", async () => {
  const cases = [
    { key: null },
    { fetchImpl: notOk(401, "unauthorized: bad key") },
    { fetchImpl: notOk(500, "upstream gemini exploded") },
    { fetchImpl: ok({ output: "", error: { message: "fal refused" } }) },
    { fetchImpl: async () => { throw new TypeError("fetch failed"); } },
  ];
  for (const options of cases) {
    await withGateway(options, async () => {
      await assert.rejects(
        () => writeQuoteText("p"),
        (err) => {
          assert.ok(!LEAKY.test(err.message), `leaked: ${err.message}`);
          return true;
        },
      );
    });
  }
});

test("every failure path writes exactly one log line", async () => {
  const cases = [
    { key: null },
    { fetchImpl: notOk(401, "no") },
    { fetchImpl: notOk(429, "no") },
    { fetchImpl: ok({ output: "", error: { message: "x" } }) },
    { fetchImpl: ok({ output: "", error: null }) },
    { fetchImpl: async () => { throw new TypeError("boom"); } },
  ];
  for (const options of cases) {
    await withGateway(options, async (logs) => {
      await assert.rejects(() => writeQuoteText("p"));
      assert.equal(logs.length, 1, `expected one line, got ${logs.length}`);
      assert.match(logs[0], /^\[quote-text\]/, "log lines must be greppable");
    });
  }
});
