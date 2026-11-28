import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const { label, LABELS, phrase } = await staticModule("lib/labels.js");
const fmt = await staticModule("lib/format.js");
const route = await staticModule("lib/route.js");

test("unknown code shows raw in neutral", () => {
  assert.deepEqual(label("phase", "rebasing"), { text: "rebasing", tone: "neutral", known: false });
  assert.deepEqual(label("phase", null), { text: "—", tone: "neutral", known: true });
  assert.equal(label("attempt_group", "accepted").tone, "green");
  assert.ok(Object.keys(LABELS).length >= 10);
});

test("prototype names are not codes", () => {
  assert.deepEqual(label("phase", "constructor"), { text: "constructor", tone: "neutral", known: false });
  assert.deepEqual(label("toString", "x"), { text: "x", tone: "neutral", known: false });
});

test("format", () => {
  assert.equal(fmt.shortId("0123456789abcdef0123456789abcdef"), "01234567");
  assert.equal(fmt.shortId("sha256:ab12cd34ef56"), "ab12cd34");
  assert.equal(fmt.shortId("demo"), "demo");
  assert.equal(fmt.number(null), "—");
  assert.equal(fmt.number(0.5), "0.500");
  assert.equal(fmt.signed(0.12), "+0.120");
  assert.equal(fmt.signed(-0.05), "−0.050");
  assert.equal(fmt.signed(0), "±0.000");
  assert.equal(fmt.duration("2026-09-30T10:00:00Z", "2026-09-30T10:01:05Z"), "1m 05s");
  assert.equal(fmt.duration("2026-09-30T10:00:00Z", "2026-09-30T10:00:42Z"), "42s");
  assert.equal(fmt.duration("2026-09-30T10:00:00Z", "2026-09-30T12:03:00Z"), "2h 03m");
  assert.equal(fmt.duration(null, "2026-09-30T10:00:00Z"), "—");
});

test("time and date are local-time shapes; missing or malformed is a dash", () => {
  assert.match(fmt.time("2026-09-30T10:00:00Z"), /^\d\d:\d\d:\d\d$/);
  assert.match(fmt.date("2026-09-30T10:00:00Z"), /^\d{4}-\d\d-\d\d \d\d:\d\d$/);
  assert.equal(fmt.time(null), "—");
  assert.equal(fmt.date("not a date"), "—");
});

test("route parse and href round-trip", () => {
  assert.deepEqual(route.parse(""), { screen: "projects", params: {} });
  assert.deepEqual(route.parse("#/projects/demo"), { screen: "project", params: { project: "demo" } });
  assert.deepEqual(route.parse("#/projects/demo/facts"), { screen: "facts", params: { project: "demo" } });
  assert.deepEqual(route.parse("#/operations/op:1"), { screen: "operation", params: { operation: "op:1" } });
  assert.deepEqual(route.parse("#/operations/o/measurements/m-r01@e1"),
    { screen: "measurement", params: { operation: "o", measurement: "m-r01@e1" } });
  assert.deepEqual(route.parse("#/operations/o/diff?from=champion:v0&to=champion:v1"),
    { screen: "diff", params: { operation: "o", from: "champion:v0", to: "champion:v1" } });
  assert.equal(route.parse("#/projects/..%2F").screen, "not_found");
  assert.equal(route.parse("#/nowhere").screen, "not_found");
  assert.equal(route.href.operation("op:1"), "#/operations/op:1");
  assert.equal(route.href.operation("bad/id"), null);
  assert.equal(route.href.measurement("o", "m-r01@e1"), "#/operations/o/measurements/m-r01@e1");
  assert.equal(route.href.diff("o", "champion:v0", "attempt:r01-a01"), "#/operations/o/diff?from=champion:v0&to=attempt:r01-a01");
});

test("other screens and rejected ids", () => {
  assert.deepEqual(route.parse("#/"), { screen: "projects", params: {} });
  assert.deepEqual(route.parse("#/projects"), { screen: "projects", params: {} });
  assert.deepEqual(route.parse("#/experiments"), { screen: "experiments", params: {} });
  assert.deepEqual(route.parse("#/states"), { screen: "states", params: {} });
  assert.deepEqual(route.parse("#/operations/o/diff?to=attempt:r01-a01"),
    { screen: "diff", params: { operation: "o", from: "initial", to: "attempt:r01-a01" } });
  assert.deepEqual(route.parse("#/operations/o/diff"),
    { screen: "diff", params: { operation: "o", from: "initial", to: null } });
  assert.equal(route.parse("#/operations/o/diff?from=../x").screen, "not_found");
  assert.equal(route.parse("#/operations/o/measurements/m@e0").screen, "not_found");
  assert.equal(route.parse("#/operations/o@e1").screen, "not_found");
  assert.equal(route.parse("#/operations/o/measurements/m-r01@e1/extra").screen, "not_found");
  assert.deepEqual(route.parse("#/nowhere"), { screen: "not_found", params: { hash: "#/nowhere" } });
  assert.equal(route.href.project("a b"), null);
  assert.equal(route.href.measurement("o", "m@e0"), null);
  assert.equal(route.href.diff("o", "initial", "bad ref"), null);
  assert.equal(route.href.projects(), "#/projects");
  assert.equal(route.href.experiments(), "#/experiments");
  assert.equal(route.href.states(), "#/states");
  assert.equal(route.href.facts("demo"), "#/projects/demo/facts");
  assert.equal(route.href.project("demo"), "#/projects/demo");
});

test("diff paths and links: bare default, validated refs, never encoded", async () => {
  const { paths } = await staticModule("lib/api.js");
  const { href } = await staticModule("lib/route.js");
  assert.equal(paths.diff("abc"), "/v1/operations/abc/diff");
  assert.equal(paths.diff("abc", "champion:v0", "attempt:r01-a01"), "/v1/operations/abc/diff?from=champion:v0&to=attempt:r01-a01");
  assert.equal(paths.diff("abc", "champion:v0", "attempt:../x"), null);
  assert.equal(href.diff("abc"), "#/operations/abc/diff");
  assert.equal(href.diff("abc", "initial", "champion:v1"), "#/operations/abc/diff?from=initial&to=champion:v1");
});

test("token counts", async () => {
  const { tokens } = await staticModule("lib/format.js");
  assert.deepEqual([1240000, 312000, 950, null].map(tokens), ["1.24M", "312k", "950", "—"]);
});

test("phrase: known codes, raw fallback", () => {
  assert.deepEqual(phrase("round_stop", "first_accepted"), { text: "stops at the first accepted finalist", known: true });
  assert.deepEqual(phrase("round_stop", "all_attempts"), { text: "all attempts run, then joint final", known: true });
  assert.deepEqual(phrase("champion_side", "stored"), { text: "Champion side: stored measurement", known: true });
  assert.deepEqual(phrase("champion_side", "fresh"), { text: "Champion re-measured in the final", known: true });
  assert.deepEqual(phrase("stability", "stable"), { text: "stable", known: true });
  assert.deepEqual(phrase("round_stop", "future_mode"), { text: "future_mode", known: false });
  assert.deepEqual(phrase("champion_side", null), { text: "null", known: false });
  assert.deepEqual(phrase("nope", "stored"), { text: "stored", known: false });
});
