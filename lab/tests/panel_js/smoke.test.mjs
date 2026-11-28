import { test } from "node:test";
import assert from "node:assert/strict";
import { canonical, snapshot } from "./support.mjs";

test("fixture keys are canonical in JS as in Python", () => {
  for (const name of ["empty", "running", "suspended-target", "suspended-role", "final"]) {
    for (const key of Object.keys(snapshot(name).routes)) assert.equal(canonical(key), key);
  }
});
