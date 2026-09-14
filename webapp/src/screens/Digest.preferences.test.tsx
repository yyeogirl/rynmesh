import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderDigest } from "../test/digestScenario";

describe("For You preferences", () => {
  it("saves written direction and reports the profile update", async () => {
    const { user, scenario, notify } = renderDigest();

    const direction = await screen.findByRole("textbox", { name: "Direction" });
    await user.type(direction, "More local AI and fewer repeated trends");
    await user.click(screen.getByRole("button", { name: "Save direction" }));

    await waitFor(() =>
      expect(scenario.requests.profilePatches).toContainEqual({
        direction: "More local AI and fewer repeated trends",
      }),
    );
    expect(notify).toHaveBeenCalledWith("ok", "Your local For You profile has been updated");
    expect(direction).toHaveValue("More local AI and fewer repeated trends");
  });

  it("selects and deselects topic preferences", async () => {
    const { user, scenario } = renderDigest();

    const localAi = await screen.findByRole("button", { name: "Local AI" });
    await user.click(localAi);
    await waitFor(() =>
      expect(scenario.requests.profilePatches).toContainEqual({ topics: ["local-ai"] }),
    );
    expect(localAi).toHaveClass("active");

    await user.click(localAi);
    await waitFor(() =>
      expect(scenario.requests.profilePatches).toContainEqual({ topics: [] }),
    );
    expect(localAi).not.toHaveClass("active");
  });

  it("selects and deselects platform preferences", async () => {
    const { user, scenario } = renderDigest();

    const youtube = await screen.findByRole("button", { name: "YouTube" });
    await user.click(youtube);
    await waitFor(() =>
      expect(scenario.requests.profilePatches).toContainEqual({ platforms: ["youtube"] }),
    );
    expect(youtube).toHaveClass("active");

    await user.click(youtube);
    await waitFor(() =>
      expect(scenario.requests.profilePatches).toContainEqual({ platforms: [] }),
    );
    expect(youtube).not.toHaveClass("active");
  });
});
