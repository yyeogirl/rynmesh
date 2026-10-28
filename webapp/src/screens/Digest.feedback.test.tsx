import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { renderDigest } from "../test/digestScenario";
import { TEST_API_BASE } from "../test/fixtures";

const ITEM_TITLE = "A local-first assistant worth reading";

describe("For You feedback and consumption", () => {
  it.each(["feedback_item_unknown", "recommendation_feedback_unavailable"])("keeps a saved reading record when feedback returns %s", async (detail) => {
    const { user, scenario } = renderDigest({ handlers: [
      http.post(`${TEST_API_BASE}/digest/feedback`, () => HttpResponse.json({ detail }, { status: detail === "feedback_item_unknown" ? 400 : 503 })),
    ] });
    await user.click(await screen.findByRole("button", { name: ITEM_TITLE }));
    await screen.findByText("This article body came from the mocked local node.");
    await waitFor(() => expect(scenario.requests.consumption).toContainEqual(expect.objectContaining({ action: "opened" })));
    if (detail !== "feedback_item_unknown") await screen.findByText("Reading history was saved, but the recommendation signal could not be updated.");
    expect(screen.queryByText(/its reading record could not be saved/)).not.toBeInTheDocument();
  });

  it("records More feedback for the selected item", async () => {
    const { user, scenario } = renderDigest();

    await screen.findByRole("button", { name: ITEM_TITLE });
    await user.click(screen.getByRole("button", { name: "More like this" }));

    await waitFor(() =>
      expect(scenario.requests.feedback).toContainEqual({ item_id: "item-article", action: "up" }),
    );
  });

  it("records Less feedback without promising to hide the article", async () => {
    const { user, scenario } = renderDigest();

    expect(await screen.findByRole("button", { name: ITEM_TITLE })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Less like this" }));

    await waitFor(() =>
      expect(scenario.requests.feedback).toContainEqual({ item_id: "item-article", action: "down" }),
    );
    expect(screen.getByRole("button", { name: ITEM_TITLE })).toBeInTheDocument();
  });

  it("removes Hide items only after the node confirms its refreshed ranking", async () => {
    const { user, scenario } = renderDigest();
    await screen.findByRole("button", { name: ITEM_TITLE });
    await user.click(screen.getByRole("button", { name: "Hide" }));
    await waitFor(() => expect(scenario.requests.feedback).toContainEqual({ item_id: "item-article", action: "hide" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: ITEM_TITLE })).not.toBeInTheDocument());
  });

  it("records opening an item and displays its viewer", async () => {
    const { user, scenario } = renderDigest();

    await user.click(await screen.findByRole("button", { name: ITEM_TITLE }));

    expect(await screen.findByRole("dialog", { name: ITEM_TITLE })).toBeInTheDocument();
    await waitFor(() =>
      expect(scenario.requests.feedback).toContainEqual({ item_id: "item-article", action: "opened" }),
    );
    await waitFor(() =>
      expect(scenario.requests.consumption).toEqual(
        expect.arrayContaining([expect.objectContaining({ action: "opened" })]),
      ),
    );
  });

  it("bookmarks and unbookmarks an opened item", async () => {
    const { user, scenario } = renderDigest();

    await user.click(await screen.findByRole("button", { name: ITEM_TITLE }));
    await screen.findByRole("dialog", { name: ITEM_TITLE });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(scenario.requests.consumption).toEqual(
        expect.arrayContaining([expect.objectContaining({ action: "bookmark" })]),
      ),
    );
    expect(screen.getByRole("button", { name: "Saved" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Saved" }));
    await waitFor(() =>
      expect(scenario.requests.consumption).toEqual(
        expect.arrayContaining([expect.objectContaining({ action: "unbookmark" })]),
      ),
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});
