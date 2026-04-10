import { describe, expect, it } from "vitest";
import { inferFocusType } from "../src/utils/marketFocus.js";

describe("inferFocusType", () => {
  it("detects bitcoin-focused markets", () => {
    expect(inferFocusType("Will Bitcoin close above $90k today?", "Crypto")).toBe(
      "bitcoin"
    );
  });

  it("detects sports markets by category", () => {
    expect(inferFocusType("Dodgers vs Padres", "Sports")).toBe("sports");
  });
});
