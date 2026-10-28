import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DigestItem } from "../domain/digestClient";
import { renderDigest } from "../test/digestScenario";
import { makeDigest, makeDigestItem } from "../test/fixtures";

async function openViewer(item: DigestItem) {
  const result = renderDigest({ digest: makeDigest([item]) });
  await result.user.click(await screen.findByRole("button", { name: item.title }));
  expect(await screen.findByRole("dialog", { name: item.title })).toBeInTheDocument();
  return result;
}

describe("DigestViewer formats", () => {
  it("opens an article using content returned by the mocked local node", async () => {
    const item = makeDigestItem();
    const { scenario } = await openViewer(item);

    expect(
      await screen.findByText("This article body came from the mocked local node."),
    ).toBeInTheDocument();
    expect(screen.getByText("Rynmesh Test Author")).toBeInTheDocument();
    await waitFor(() => expect(scenario.requests.feedback).toContainEqual({ item_id: item.item_id, action: "opened" }));
  });

  it("opens an image item without fetching live media", async () => {
    const mediaUrl = "data:image/png;base64,iVBORw0KGgo=";
    const item = makeDigestItem({
      item_id: "item-image",
      title: "A generated local image",
      link: "https://example.test/image",
      content_kind: "image",
      content_type: "image/png",
      media_url: mediaUrl,
    });

    await openViewer(item);

    expect(screen.getByRole("img", { name: item.title })).toHaveAttribute("src", mediaUrl);
  });

  it("opens an audio item with a non-autoplay local fixture source", async () => {
    const mediaUrl = "data:audio/mpeg;base64,SUQz";
    const item = makeDigestItem({
      item_id: "item-audio",
      title: "A local audio briefing",
      link: "https://example.test/audio",
      content_kind: "audio",
      content_type: "audio/mpeg",
      media_url: mediaUrl,
    });

    const { container } = await openViewer(item);
    const audio = container.querySelector("audio");

    expect(audio).toHaveAttribute("src", mediaUrl);
    expect(audio).toHaveAttribute("preload", "none");
    expect(audio).not.toHaveAttribute("autoplay");
  });

  it("opens YouTube through the privacy-enhanced embed URL without live playback", async () => {
    const item = makeDigestItem({
      item_id: "item-video",
      title: "A local-first YouTube explainer",
      link: "https://www.youtube.com/watch?v=abc123fixture",
      content_kind: "video",
      content_type: "text/html",
    });

    await openViewer(item);

    expect(screen.getByTitle(item.title)).toHaveAttribute(
      "src",
      "https://www.youtube-nocookie.com/embed/abc123fixture",
    );
    expect(
      screen.getAllByRole("link", { name: /Original/ }).some((link) => link.getAttribute("href") === item.link),
    ).toBe(true);
  });
});
