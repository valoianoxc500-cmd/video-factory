/**
 * Input validation and error shaping in the repository layer.
 *
 * The repositories are the only place routes touch data, so the checks that
 * keep a request from reaching the database in a bad shape live here. The
 * fake client records what would have been sent, which is how ownership is
 * verified: the job insert must carry the session's user id, never the
 * caller's.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const repos = require("../.test-build/repositories.js");

/** Minimal stand-in for the Supabase client, capturing the insert payload. */
function fakeDb(captured) {
  const builder = {
    select: () => builder,
    eq: () => builder,
    in: () => builder,
    order: () => builder,
    limit: () => builder,
    maybeSingle: async () => ({ data: null, error: null }),
    single: async () => ({ data: captured.payload, error: null }),
    then: (resolve) => resolve({ data: [], error: null }),
  };
  return {
    from() {
      return {
        ...builder,
        insert(payload) {
          captured.payload = payload;
          return builder;
        },
      };
    },
  };
}

test("known channels are accepted", () => {
  assert.equal(repos.assertChannelSlug("horror_stories"), "horror_stories");
  assert.equal(repos.assertChannelSlug("football_news"), "football_news");
});

test("an unknown or malformed channel is refused", () => {
  const bad = [
    "horror",
    "Horror_Stories",
    "horror_stories; drop table videos",
    "../football_news",
    "",
    "a".repeat(200),
    "football_news ",
  ];
  for (const slug of bad) {
    assert.throws(
      () => repos.assertChannelSlug(slug),
      /Unknown channel/,
      `should have refused: ${JSON.stringify(slug)}`,
    );
  }
});

test("a job is filed against the session user, not a supplied id", async () => {
  const captured = {};
  const repo = new repos.JobRepository(fakeDb(captured));
  await repo.create({
    userId: "session-user-id",
    topic: "  D.B. Cooper  ",
    channel: "horror_stories",
  });
  assert.equal(captured.payload.user_id, "session-user-id");
  assert.equal(captured.payload.topic, "D.B. Cooper", "topic should be trimmed");
  assert.equal(captured.payload.channel_slug, "horror_stories");
  assert.equal(captured.payload.status, "queued");
});

test("an empty topic is refused before reaching the database", async () => {
  const captured = {};
  const repo = new repos.JobRepository(fakeDb(captured));
  await assert.rejects(
    () => repo.create({ userId: "u", topic: "   ", channel: "horror_stories" }),
    /topic is required/i,
  );
  assert.equal(captured.payload, undefined, "nothing should have been sent");
});

test("an overlong topic is refused", async () => {
  const repo = new repos.JobRepository(fakeDb({}));
  await assert.rejects(
    () => repo.create({ userId: "u", topic: "x".repeat(301), channel: "horror_stories" }),
    /300 characters/,
  );
});

test("a job for an unknown channel is refused", async () => {
  const repo = new repos.JobRepository(fakeDb({}));
  await assert.rejects(
    () => repo.create({ userId: "u", topic: "ok", channel: "not_a_channel" }),
    /Unknown channel/,
  );
});

test("a malformed story type is refused", async () => {
  const repo = new repos.JobRepository(fakeDb({}));
  await assert.rejects(
    () =>
      repo.create({
        userId: "u",
        topic: "ok",
        channel: "horror_stories",
        style: "true story; drop table videos",
      }),
    /Unknown story type/,
  );
});

test("a missing record reads as not found, not as a server error", () => {
  const err = new repos.NotFoundError();
  assert.equal(repos.toHttpError(err).status, 404);
});

test("a driver message is never returned to the caller", () => {
  const err = new repos.RepositoryError(
    'relation "public.videos" does not exist at character 15',
  );
  const shaped = repos.toHttpError(err);
  assert.equal(shaped.status, 500);
  assert.doesNotMatch(shaped.message, /relation|public\.videos|character/i);
});

test("an unexpected error does not leak its contents", () => {
  const shaped = repos.toHttpError(new Error("connect ECONNREFUSED 10.0.0.5:5432"));
  assert.equal(shaped.status, 500);
  assert.doesNotMatch(shaped.message, /ECONNREFUSED|10\.0\.0\.5/);
});

test("every advertised channel has a slug the validator accepts", () => {
  assert.ok(repos.CHANNELS.length >= 2);
  for (const channel of repos.CHANNELS) {
    assert.equal(repos.assertChannelSlug(channel.slug), channel.slug);
    assert.ok(channel.name && channel.theme);
  }
});
