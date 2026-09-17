import { useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Panel } from "../components/ui";
import type { InferenceAccess as Access } from "../domain/nodeClient";

export default function InferenceAccess() {
  const { client, notify } = useAppContext();
  const [access, setAccess] = useState<Access | null>(null);
  const [name, setName] = useState("My project");
  const [limit, setLimit] = useState("100000");
  const [key, setKey] = useState("");
  const [selected, setSelected] = useState("");
  const [alias, setAlias] = useState("qwen");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<void>) {
    setBusy(true);
    try { await action(); } catch (error) { notify("danger", error instanceof Error ? error.message : "API access failed"); }
    finally { setBusy(false); }
  }
  async function refresh() { setAccess(await client.getInferenceAccess()); }
  const model = access?.models.some((item) => item.id === selected) ? selected : access?.models[0]?.id || "MODEL_ID_FROM_LIST";
  const aliasTarget = target || access?.aliases[alias] || access?.targets[0]?.id || "";
  const sample = `from openai import OpenAI\nimport os\n\nclient = OpenAI(\n    base_url=${JSON.stringify(access?.base_url)},\n    api_key=os.environ["RYNMESH_API_KEY"],\n)\nresult = client.chat.completions.create(\n    model=${JSON.stringify(model)},\n    messages=[{"role": "user", "content": "Hello"}],\n    max_tokens=64,\n)\nprint(result.choices[0].message.content)`;
  async function copy(text: string) { await navigator.clipboard.writeText(text); notify("ok", "Copied"); }
  return <Panel>
    <div className="panel-head"><div><span className="eyebrow">For projects and agents</span><h2>API access</h2></div>
      <Button disabled={busy} onClick={() => void run(refresh)}>{access ? "Refresh API access" : "Configure API access"}</Button></div>
    <p>Call local or remote models through this node. Remote model calls use strict P2P with no HTTP or relay fallback. API keys grant inference access only. Connections are accepted from this computer.</p>
    {access && <div className="screen-stack" style={{ minWidth: 0 }}>
      <p>OpenAI base URL: <code>{access.base_url}</code></p>
      <p>Anthropic base URL: <code>{access.base_url.replace(/\/v1$/, "")}</code>. Responses supports stateless text and function tools; send conversation history with each request.</p>
      <label>Model alias<input value={alias} onChange={(event) => { setAlias(event.target.value); setTarget(""); }} placeholder="qwen" maxLength={64} /></label>
      <label>Routes to<select value={aliasTarget} onChange={(event) => setTarget(event.target.value)}>
        {access.aliases[alias] && !access.targets.some((item) => item.id === access.aliases[alias]) && <option value={access.aliases[alias]}>Saved target · unavailable</option>}
        {access.targets.map((item) => <option key={item.id} value={item.id}>{item.rynmesh.model_alias} · {item.rynmesh.source}{item.rynmesh.source === "peer" ? ` ${item.id.split("/")[1].slice(0, 8)}` : ""}</option>)}
      </select></label>
      <Button disabled={busy || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/.test(alias) || !access.targets.some((item) => item.id === aliasTarget)} onClick={() => void run(async () => { await client.setInferenceModelAlias(alias, aliasTarget); setSelected(alias); await refresh(); notify("ok", `Use ${alias} as the model in your agent.`); })}>Save model alias</Button>
      <p>Aliases are saved on this node. Your projects use the short name; the node routes each call to the selected model.</p>
      {Object.entries(access.aliases).map(([name, id]) => {
        const item = access.targets.find((candidate) => candidate.id === id);
        return <p key={name}><code>{name}</code> → {item ? `${item.rynmesh.model_alias} · ${item.rynmesh.source}` : "Saved target · unavailable"} <Button disabled={busy} onClick={() => { setAlias(name); setTarget(id); }}>Edit {name}</Button></p>;
      })}
      <label>Model<select value={model} onChange={(event) => setSelected(event.target.value)}>
        {access.models.map((item) => <option key={item.id} value={item.id}>{item.id.includes("/") ? `${item.rynmesh.model_alias} · ${item.rynmesh.source} · ${item.id.split("/")[1].slice(0, 8)}` : item.id}</option>)}
      </select></label>
      {!access.models.length && <p>No API-ready model is available. Configure a local model or update and publish a remote provider.</p>}
      <label>Project name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} /></label>
      <label>Total output-token budget<input type="number" min="1" max="1000000000" value={limit} onChange={(event) => setLimit(event.target.value)} /></label>
      <Button disabled={busy || !name.trim() || Number(limit) < 1} onClick={() => void run(async () => { const value = await client.createInferenceKey(name, Number(limit)); setKey(value.key); await refresh(); })}>Create API key</Button>
      {key && <div><p>Save this key now. It is shown only once.</p><code>{key}</code> <Button onClick={() => void run(() => copy(key))}>Copy key</Button> <Button onClick={() => setKey("")}>Hide key</Button></div>}
      {access.keys.map((item) => <div key={item.id}><strong>{item.name}</strong> · {item.used_output_tokens.toLocaleString()} / {item.output_token_limit.toLocaleString()} output tokens · {item.revoked ? "Revoked" : "Active"}
        {!item.revoked && <Button disabled={busy} onClick={() => void run(async () => { await client.revokeInferenceKey(item.id); setKey(""); await refresh(); })}>Revoke {item.name}</Button>}</div>)}
      <p>Remote calls use the node’s development Task Balance. Failed or interrupted requests with unknown usage retain their reserved output-token budget.</p>
      <pre style={{ maxWidth: "100%", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}><code>{sample}</code></pre>
      <Button onClick={() => void run(() => copy(sample))}>Copy Python example</Button>
    </div>}
  </Panel>;
}
