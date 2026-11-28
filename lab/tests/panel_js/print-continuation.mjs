// Prints the panel's continuation line for [status, readyTarget] given as JSON in argv
// (tests/test_panel_continuation.py compares it with the CLI's).
import { staticModule } from "./support.mjs";
const { continuationLine } = await staticModule("lib/continuation.js");
if (process.argv[2] === "suspended") {
  // The suspended screen's model: the card's line for one fixture operation (argv: suspended <fixture> <id>).
  const { suspendedModel } = await staticModule("lib/views/suspended.js");
  const { okEntry } = await staticModule("lib/resource.js");
  const { body } = await import("./support.mjs");
  const [, , , name, id] = process.argv;
  const operation = body(name, `/v1/operations/${id}`);
  const report = okEntry(body(name, `/v1/operations/${id}/report`));
  process.stdout.write(JSON.stringify(suspendedModel({ operation, campaign: undefined, report }).card.command));
} else {
  const [status, ready] = JSON.parse(process.argv[2]);
  process.stdout.write(JSON.stringify(continuationLine(status, ready)));
}
