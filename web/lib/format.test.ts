import { describe, expect, it } from "vitest";
import { formatMoney, formatPercent } from "./format";

describe("format helpers", () => {
  it("formats usd values", () => {
    expect(formatMoney(1234.56)).toContain("$1,234.56");
  });

  it("formats percentages", () => {
    expect(formatPercent(12.345, 2)).toBe("12.35%");
  });
});
