// screens/operation.js — #/operations/<O>: the evaluation, suspended-campaign or campaign screen, chosen by
// the operation itself (lib/views/campaign.js screenOf). The operation read is shared with the chosen screen.
import { html } from "../vendor/htm-preact.js";
import { paths } from "../lib/api.js";
import { shortId } from "../lib/format.js";
import { href } from "../lib/route.js";
import { readOf } from "../lib/resource.js";
import { screenOf } from "../lib/views/campaign.js";
import { mergeLive, overlayFor } from "../lib/views/shell.js";
import { useLiveRefresh, useResource } from "../ui/hooks.js";
import { ReadState } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { CampaignScreen } from "./campaign.js";
import { EvaluationScreen } from "./evaluation.js";
import { SuspendedScreen } from "./suspended.js";

export function OperationScreen(props) {
  const { api, epoch, outage, live, route } = props;
  const id = route.params.operation;
  const refresh = useLiveRefresh(live, id);
  const operation = useResource(api, paths.operation(id), { epoch, outage, refresh });
  const routing = useResource(api, paths.routing(id), { epoch, outage, refresh });
  const data = mergeLive(operation.data, overlayFor(live, operation.epoch));
  if (data === undefined) return html`<div class="screen" data-screen="operation">
    <${Crumbs} items=${[{ text: "Experiments", href: href.experiments() }, { text: shortId(id), href: null }]} />
    <${ReadState} read=${readOf(operation)} loading="Loading the experiment…" /></div>`;
  const Screen = { evaluation: EvaluationScreen, suspended: SuspendedScreen, campaign: CampaignScreen }[screenOf(data)];
  return html`<${Screen} ...${props} operation=${operation} data=${data} refresh=${refresh} routing=${routing} />`;
}
