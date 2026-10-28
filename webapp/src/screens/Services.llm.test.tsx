import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type {
  LLMHardwareReport,
  LLMOrderResult,
  LLMProviderStatus,
  LLMServiceRecord,
  LLMSetupJob,
} from "../domain/nodeClient";
import Services from "./Services";

function renderServices(options: {
  configuredNetwork?: string;
  discoveryFailure?: boolean;
  services?: LLMServiceRecord[];
  providerStatus?: LLMProviderStatus;
  orders?: LLMOrderResult[];
  setupStatuses?: LLMSetupJob[];
  hardware?: LLMHardwareReport;
} = {}) {
  const client = makeFixtureNodeClient();
  if (options.hardware) client.getLLMHardware = vi.fn(async () => options.hardware!);
  const discover = vi.spyOn(client, "listLLMServices");
  if (options.configuredNetwork) {
    const getSettings = client.getSettings.bind(client);
    client.getSettings = vi.fn(async () => ({
      ...await getSettings(),
      network_id: options.configuredNetwork,
    }));
  }
  if (options.discoveryFailure) {
    discover.mockImplementation(async () => {
      throw new Error("LLM discovery unavailable");
    });
  } else if (options.services) {
    discover.mockResolvedValue(options.services);
  }
  if (options.providerStatus) client.getLLMServiceStatus = vi.fn(async () => options.providerStatus!);
  if (options.orders) client.listLLMOrders = vi.fn(async () => options.orders!);
  if (options.setupStatuses) {
    let index = 0;
    client.getLLMSetupStatus = vi.fn(async () => (
      options.setupStatuses![Math.min(index++, options.setupStatuses!.length - 1)]
    ));
  }
  const submit = vi.spyOn(client, "submitLLMOrder");
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [],
    refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(),
    notify: vi.fn(),
  };
  const result = render(
    <MemoryRouter>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route index element={<Services />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { ...result, client, discover, submit, confirm: context.confirm, notify: context.notify, user: userEvent.setup() };
}

describe("Services local LLM flow", () => {
  it.each([
    ["local inference runtime dependency is missing; use Update runtime to repair it", /A local runtime dependency is missing.*Update runtime/],
    ["configured model file is missing", /The selected model file is missing/],
    ["the local inference runtime is not installed", /The local runtime is missing/],
  ])("keeps an actionable lifecycle error until retry: %s", async (message, expected) => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0].service;
    const { client, user } = renderServices({ providerStatus: { configured: true, online: false, service,
      lifecycle: { mode: "managed", runtime: { managed: true, running: false } } } });
    const action = vi.spyOn(client, "runLLMServiceAction").mockRejectedValueOnce(new Error(String(message)));
    await user.click(await screen.findByRole("button", { name: "Start runtime" }));
    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent(expected);
    expect(error).toHaveFocus();
    await waitFor(() => expect(screen.getByRole("button", { name: "Start runtime" })).toBeEnabled());
    action.mockResolvedValue({ ok: true });
    await user.click(screen.getByRole("button", { name: "Start runtime" }));
    await waitFor(() => expect(screen.queryByText(expected)).not.toBeInTheDocument());
    expect(action).toHaveBeenCalledTimes(2);
  });

  it("updates model storage after confirmed deletion and reports preserved shared runtime files", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0].service;
    const providerStatus: LLMProviderStatus = { configured: true, online: false, service,
      lifecycle: { mode: "managed", runtime: { managed: true, running: false },
        storage: { model_owned: true, model_present: true, model_bytes: 1048576 } } };
    const { client, user, confirm, notify } = renderServices({ providerStatus });
    const action = vi.spyOn(client, "runLLMServiceAction").mockResolvedValue({ result: { removed: ["runtime_process", "managed_model"], model_preserved: false } });
    expect(await screen.findByText(/Model file: 1.0 MiB/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete managed model" }));
    const review = vi.mocked(confirm).mock.calls[0][0];
    expect(review.body).toContain("Shared native runtime files, private configuration and conversations are preserved");
    expect(action).not.toHaveBeenCalled();
    vi.mocked(client.getLLMServiceStatus).mockResolvedValue({ ...providerStatus, lifecycle: { ...providerStatus.lifecycle,
      storage: { model_owned: true, model_present: false, model_bytes: 0 } } });
    await act(async () => { await review.onConfirm(); });
    expect(await screen.findByText(/Model file: missing · 0 MiB/)).toBeInTheDocument();
    expect(action).toHaveBeenCalledWith("uninstall", { delete_environment: true, delete_model: true, confirm_model_delete: true });
    expect(notify).toHaveBeenCalledWith("ok", expect.stringContaining("shared runtime files remain installed"));
  });

  it("restores the managed model choices after restart and requires a fresh confirmation", async () => {
    const { client, user } = renderServices({ setupStatuses: [{
      job_id: "setup_managed_resume", state: "cancelled", stage: "cancelled", progress: 0, retryable: true,
      resume_configuration: { mode: "managed", profile: "light", package_id: "previous-model", port: 18925 },
    }] });
    expect(await screen.findByLabelText("Setup mode")).toHaveValue("managed");
    expect(screen.getByLabelText("Model profile")).toHaveValue("light");
    expect(screen.getByLabelText("Package ID")).toHaveValue("previous-model");
    expect(screen.getByLabelText("Local runtime port")).toHaveValue("18925");
    const confirm = screen.getByRole("checkbox", { name: /I understand this prepares a local runtime/ });
    expect(confirm).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Retry configuration" })).toBeDisabled();
    const start = vi.spyOn(client, "startLLMSetup");
    expect(start).not.toHaveBeenCalled();
    await user.click(confirm);
    await user.click(screen.getByRole("button", { name: "Retry configuration" }));
    expect(start).toHaveBeenCalledWith(expect.objectContaining({
      mode: "managed", profile: "light", package_id: "previous-model", port: 18925, accept_risk: true,
    }));
  });

  it("requires choosing a setup mode when an old failed job has no saved choices", async () => {
    renderServices({ setupStatuses: [{ job_id: "legacy", state: "failed", stage: "recovery", progress: 0, retryable: true }] });
    expect(await screen.findByLabelText("Setup mode")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Retry configuration" })).toBeDisabled();
    expect(screen.getByText(/Previous setup choices are unavailable/)).toBeInTheDocument();
  });

  it("waits for recovery after cancellation and offers retry when restoration fails", async () => {
    const { client, user, notify } = renderServices({ setupStatuses: [
      { job_id: "setup_restore", state: "running", stage: "download_model", progress: 40 },
    ] });
    let failed = false;
    let cancelled = false;
    client.getLLMSetupStatus = vi.fn(async (): Promise<LLMSetupJob> => failed ? {
      job_id: "setup_restore", state: "failed", stage: "recovery", progress: 0, retryable: true,
      message: "Previous configuration could not be restored. Check local storage and retry configuration.",
    } : { job_id: "setup_restore", state: cancelled ? "cancelling" : "running", stage: "download_model", progress: 40 });
    client.cancelLLMSetup = vi.fn(async (): Promise<LLMSetupJob> => {
      cancelled = true;
      return { job_id: "setup_restore", state: "cancelling", stage: "cancelling", progress: 40 };
    });
    await user.click(await screen.findByRole("button", { name: "Cancel setup" }));
    expect(client.cancelLLMSetup).toHaveBeenCalledWith("setup_restore");
    expect(notify).toHaveBeenCalledWith("warn", "Setup cancellation requested. Wait for the final recovery status.");
    expect(await screen.findByRole("button", { name: "Cancelling…" })).toBeDisabled();
    failed = true;
    expect(await screen.findByText(/Previous configuration could not be restored/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry configuration" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Setup mode"), "openai-compatible");
    expect(screen.getByRole("button", { name: "Retry configuration" })).toBeEnabled();
    const retry = vi.spyOn(client, "startLLMSetup");
    await user.click(screen.getByRole("button", { name: "Retry configuration" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("offers a ready unpublished model directly in Ask Ryn", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0].service;
    renderServices({ providerStatus: { configured: true, ready: true, online: false, publication_enabled: false, service, capacity: { available: 1, max_concurrent: 1 } } });
    const link = await screen.findByRole("link", { name: "Ask using this device" });
    expect(link).toHaveAttribute("href", expect.stringContaining("peer=peer%3Atest"));
    expect(link).toHaveAttribute("href", expect.stringContaining(encodeURIComponent(service.package_id)));
    expect(screen.getByText("ready on this device")).toBeInTheDocument();
    expect(screen.getByText(/Remote sharing is off/)).toBeInTheDocument();
  });

  it("reviews the model source and license and pins the automatic choice before installation", async () => {
    const { user, client } = renderServices({ hardware: { hardware: { native_runtime_available: true }, recommendations: [
      { profile: "light", can_run: true, recommended: true, display_name: "Reviewed model", download_bytes: 512 * 1024 * 1024, estimated_disk_mb: 1200, estimated_memory_mb: 900, source_url: "https://example.test/pinned-model.gguf", license_id: "Apache-2.0", license_url: "https://www.apache.org/licenses/LICENSE-2.0", license_notice: "Review the license before use." },
    ] } });
    const setup = vi.spyOn(client, "startLLMSetup");
    await screen.findByRole("heading", { name: "Ryn job capacity" });
    await user.selectOptions(screen.getByLabelText("Setup mode"), "managed");
    expect(await screen.findByRole("link", { name: "Pinned model source" })).toHaveAttribute("href", "https://example.test/pinned-model.gguf");
    expect(screen.getByRole("link", { name: "License: Apache-2.0" })).toBeInTheDocument();
    expect(screen.getByText(/Model download: 512 MiB; required disk: 1200 MiB/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Configure and run self-test" })).toBeDisabled();
    expect(setup).not.toHaveBeenCalled();
    await user.click(screen.getByRole("checkbox", { name: /prepares a local runtime/i }));
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));
    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({ mode: "managed", profile: "light" })));
  });

  it("uses the production network default and submits the selected transport policy", async () => {
    const { user, submit } = renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByLabelText("Discovery network")).toHaveValue("rynmesh-main");
    await user.selectOptions(screen.getByLabelText("Transport policy"), "p2p");
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    expect(await screen.findByText(/Strict P2P connection is in progress/)).toBeInTheDocument();
    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      network_id: "rynmesh-main",
      transport: "p2p",
    })));
    expect(await screen.findByText("ice_udp_direct")).toBeInTheDocument();
  });

  it("uses the node's configured network for initial discovery", async () => {
    const { discover } = renderServices({ configuredNetwork: "rynmesh-llm-e2e" });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByLabelText("Discovery network")).toHaveValue("rynmesh-llm-e2e");
    expect(discover).toHaveBeenCalledWith("rynmesh-llm-e2e");
  });

  it("disambiguates equal aliases and submits the exact node and package", async () => {
    const commonService = {
      model_alias: "p2p-direct-host-private-model",
      capabilities: ["chat"],
      context_window: 4096,
      max_output_tokens: 128,
      pricing: {
        currency: "DEV_TASK_BALANCE",
        input_per_1k: 0,
        output_per_1k: 0,
        minimum: 0.001,
        maximum_per_task: 0.01,
      },
      privacy: { policy_text: "Provider sees plaintext during inference." },
    };
    const services: LLMServiceRecord[] = [
      {
        peer_id: "peer-docker",
        node_name: "docker-provider",
        online: true,
        service: { ...commonService, package_id: "e2e-host-real-service" },
      },
      {
        peer_id: "peer-host",
        node_name: "host-native-provider",
        online: true,
        service: { ...commonService, package_id: "e2e-p2p-host-real-service" },
      },
    ];
    const { user, submit } = renderServices({ services });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    const provider = screen.getByLabelText("Provider service");
    expect(screen.getByRole("option", { name: /docker-provider.*e2e-host-real-service/ })).toBeInTheDocument();
    await user.selectOptions(
      provider,
      screen.getByRole("option", { name: /host-native-provider.*e2e-p2p-host-real-service/ }),
    );
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      provider_peer_id: "peer-host",
      service_id: "e2e-p2p-host-real-service",
    })));
  });

  it("explains that an older different-egress package must be updated", async () => {
    const { client, user } = renderServices();
    client.submitLLMOrder = vi.fn(async () => ({
      task_id: "task_shared_exit",
      state: "failed",
      error_code: "p2p_distinct_public_egress_required",
    }));

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    expect(await screen.findByText(/incorrectly requires different public exits/)).toBeInTheDocument();
  });

  it("keeps the Services screen usable when LLM discovery fails", async () => {
    renderServices({ discoveryFailure: true });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByText(/Service discovery failed: LLM discovery unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("configures a local API without publishing it automatically", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "startLLMSetup");
    const publish = vi.spyOn(client, "publishLLMService");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Package ID"));
    await user.type(screen.getByLabelText("Package ID"), "my-local-api");
    await user.clear(screen.getByLabelText("Local API URL"));
    await user.type(screen.getByLabelText("Local API URL"), "http://127.0.0.1:9999");
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      mode: "openai-compatible",
      package_id: "my-local-api",
      base_url: "http://127.0.0.1:9999",
      accept_risk: false,
    })));
    expect(publish).not.toHaveBeenCalled();
  });

  it("polls an asynchronous order and sends cancellation", async () => {
    const { client, user } = renderServices();
    let cancelled = false;
    client.submitLLMOrder = vi.fn(async () => ({ task_id: "task_async", state: "queued" }));
    client.getLLMOrder = vi.fn(async () => ({
      task_id: "task_async", state: cancelled ? "cancelled" : "running",
    }));
    client.cancelLLMOrder = vi.fn(async () => {
      cancelled = true;
      return { task_id: "task_async", state: "cancelled" };
    });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));
    await user.click(await screen.findByRole("button", { name: "Cancel task" }));

    expect(client.cancelLLMOrder).toHaveBeenCalledWith("task_async");
    expect(await screen.findByText(/cancellation requested/)).toBeInTheDocument();
    await waitFor(() => expect(client.getLLMOrder).toHaveBeenCalledWith("task_async"), { timeout: 2000 });
  });

  it("submits authenticated trusted-network local API settings without secret values", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "startLLMSetup");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("API key environment variable (optional)"), "LOCAL_MODEL_KEY");
    await user.click(screen.getByRole("checkbox", { name: /trusted non-loopback API address/i }));
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      api_key_env: "LOCAL_MODEL_KEY",
      allow_non_loopback: true,
    })));
  });

  it("resumes polling a running task discovered after page load", async () => {
    const running = { task_id: "task_resume_after_reload", state: "running" };
    const { client } = renderServices({ orders: [running] });
    client.getLLMOrder = vi.fn(async () => ({
      task_id: running.task_id,
      state: "succeeded",
      output: "resumed result",
      transport: "ice_udp_direct" as const,
    }));

    expect(await screen.findByText("resumed result")).toBeInTheDocument();
    expect(client.getLLMOrder).toHaveBeenCalledWith(running.task_id);
  });

  it("shows recovered setup progress and Provider lifecycle controls", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0].service;
    const providerStatus: LLMProviderStatus = {
      configured: true,
      online: true,
      publication_enabled: false,
      service,
      lifecycle: { runtime: { managed: true, installed: true, running: true, status: "running" } },
    };
    const { client, user } = renderServices({
      providerStatus,
      setupStatuses: [
        { job_id: "setup_resume", state: "running", stage: "download_model", progress: 55, message: "Downloading model data; verification pending" },
        { job_id: "setup_resume", state: "succeeded", stage: "completed", progress: 100, message: "Local model is ready" },
      ],
    });
    const action = vi.spyOn(client, "runLLMServiceAction");

    expect(await screen.findByText("Local model is ready")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run self-test" }));
    expect(action).toHaveBeenCalledWith("self-test");
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart" })).toBeEnabled());
  });

  it("blocks an order before submission when the Provider is offline", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0];
    const { submit } = renderServices({ services: [{ ...service, online: false }] });

    expect((await screen.findAllByText("Availability unknown — refresh services")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Place encrypted order" })).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("keeps a stopped local model distinct from an offline node and offers runtime recovery", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0];
    const { user, client, submit } = renderServices({
      services: [{ ...service, access: "self", ready: false, online: false }],
      providerStatus: { configured: true, ready: false, online: false, service: service.service,
        lifecycle: { runtime: { installed: true, running: false, status: "stopped" } } },
    });
    expect((await screen.findAllByText("Local model not ready — start it or check model settings")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Provider offline")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Place encrypted order" })).toBeDisabled();
    const start = vi.spyOn(client, "runLLMServiceAction");
    await user.click(screen.getByRole("button", { name: "Start runtime" }));
    expect(start).toHaveBeenCalledWith("start");
    expect(submit).not.toHaveBeenCalled();
  });

  it("labels the managed setup option as a bundled-runtime local model, not Docker", async () => {
    renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Managed local model (bundled runtime)" })).toBeInTheDocument();
    expect(screen.queryByText(/Optional managed Docker model/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Docker Desktop\/Engine must already be installed and running/)).not.toBeInTheDocument();
  });

  it("shows the bundled runtime helper text and profile select for managed setup", async () => {
    const { user } = renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Setup mode"), "managed");

    expect(await screen.findByText(
      "Downloads a verified model and runs it with the bundled llama.cpp runtime on this device. Docker is only used on server nodes that choose it.",
    )).toBeInTheDocument();
    expect(screen.getByLabelText("Model profile")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Balanced.*recommended/ })).toBeInTheDocument();
  });

  it("reports the runtime actually on the device, not one that could be downloaded", async () => {
    // The fixture node reports `native_runtime_available: true` with
    // `native_runtime_present: false`; branching on the wrong field would
    // claim the runtime is already there.
    renderServices();

    expect(await screen.findByText("Bundled runtime: will be downloaded on first setup"))
      .toBeInTheDocument();
    expect(screen.queryByText("Bundled runtime: available")).not.toBeInTheDocument();
  });

  it("shows the bundled runtime as available once it is present on the device", async () => {
    renderServices({
      hardware: {
        hardware: { native_runtime_available: true, native_runtime_present: true },
        recommendations: [{ profile: "light", can_run: true, display_name: "Light" }],
      },
    });

    expect(await screen.findByText("Bundled runtime: available")).toBeInTheDocument();
  });

  it("leaves no blank profile option when nothing in the catalog fits", async () => {
    // The node's no-fit sentinel carries `can_run: false` and no profile
    // name, so an unfiltered map renders an empty <option>.
    const { user } = renderServices({
      hardware: {
        hardware: { native_runtime_available: true, native_runtime_present: true },
        recommendations: [
          { can_run: false, reason: "No bundled profile safely fits detected available RAM/disk." },
        ] as unknown as LLMHardwareReport["recommendations"],
      },
    });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Setup mode"), "managed");

    const options = screen.getByLabelText("Model profile").querySelectorAll("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveValue("auto");
  });

  it("posts the selected profile in the managed setup body", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "startLLMSetup");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Setup mode"), "managed");
    await user.selectOptions(screen.getByLabelText("Model profile"), "balanced");
    await user.click(screen.getByRole("checkbox", { name: /prepares a local runtime/i }));
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      mode: "managed",
      profile: "balanced",
    })));
  });

  it.each([
    {
      raw: "download incomplete; retry to resume",
      mapped: "The download was interrupted. Downloaded data is kept; retry to continue. The model still needs verification.",
    },
    {
      raw: "download response has invalid byte range or encoding; retry to resume",
      mapped: "The source returned an invalid download response. Your previous progress is kept; retry when the source is available.",
    },
    {
      raw: "no local inference runtime is available: nothing resolvable",
      mapped: "No local inference runtime is available on this device yet. Retry to download the bundled runtime, or connect an existing local model API.",
    },
    {
      raw: "runtime archive checksum mismatch",
      mapped: "A download failed verification and was discarded. Retry to download it again.",
    },
    {
      raw: "model checksum mismatch; the download was quarantined and will restart",
      mapped: "A download failed verification and was discarded. Retry to download it again.",
    },
    {
      raw: "llama-server exited during startup (see the runtime log)",
      // No "runtime log" view exists to send the owner to, so the copy only
      // names an action they can actually take.
      mapped: "The local model runtime stopped while starting. Retry with a smaller model profile.",
    },
    {
      raw: "download exceeded the pinned size",
      mapped: "The download did not match the expected size and was discarded. Retry.",
    },
  ])("maps the native runtime error '$raw'", async ({ raw, mapped }) => {
    renderServices({
      setupStatuses: [
        { job_id: "setup_native_error", state: "failed", stage: "download_model", progress: 40, message: raw, retryable: true },
      ],
    });

    expect(await screen.findByText(mapped)).toBeInTheDocument();
  });
});
