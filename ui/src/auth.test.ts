import { afterEach, describe, expect, it, vi } from "vitest";
import PocketBase from "pocketbase";
import { app } from "./config";
import { pb, query, refreshSession, signIn } from "./api";
const record = {
  id: "synthetic000001",
  collectionId: "synthetic",
  collectionName: app.authCollection,
};
const token = "test." + btoa(JSON.stringify({ exp: 4102444800 })) + ".test";
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  pb.authStore.clear();
});
describe("session race protection", () => {
  it("rejects a successful old query even when the same token is restored", async () => {
    pb.authStore.save(token, record);
    vi.spyOn(pb, "send").mockImplementation(async () => {
      pb.authStore.clear();
      pb.authStore.save(token, record);
      return {
        columns: ["private"],
        rows: [["old response"]],
        truncated: false,
      };
    });
    await expect(query("SELECT id FROM records")).rejects.toThrow(
      "Session changed",
    );
  });
  it("a late login cannot overwrite a different tab's account before its storage event", async () => {
    pb.authStore.clear();
    vi.spyOn(PocketBase.prototype, "send").mockImplementation(async () => {
      // LocalAuthStore reads token from storage synchronously; no storage event yet.
      localStorage.setItem(
        app.name + ".reader.auth",
        JSON.stringify({ token: "new-account", record }),
      );
      return { token, record };
    });
    await expect(
      signIn("synthetic@example.com", "test-password"),
    ).rejects.toThrow("Session changed");
    expect(pb.authStore.token).toBe("new-account");
  });
  it("a late renewal cannot revive a signed-out session", async () => {
    vi.spyOn(Date, "now").mockReturnValue(2000000000000);
    pb.authStore.save(token, record);
    vi.spyOn(PocketBase.prototype, "send").mockImplementation(async () => {
      pb.authStore.clear();
      return { token, record };
    });
    await refreshSession();
    expect(pb.authStore.token).toBe("");
  });
  it("refreshes at most once per five minutes, including network failures", async () => {
    vi.spyOn(Date, "now").mockReturnValue(2000000600000);
    pb.authStore.save(token, record);
    const request = vi
      .spyOn(PocketBase.prototype, "send")
      .mockRejectedValue({ status: 0 });
    await refreshSession();
    await refreshSession();
    expect(request).toHaveBeenCalledTimes(1);
    expect(pb.authStore.token).toBe(token);
  });
  it("clears revoked credentials on refresh", async () => {
    vi.spyOn(Date, "now").mockReturnValue(2000001200000);
    pb.authStore.save(token, record);
    vi.spyOn(PocketBase.prototype, "send").mockRejectedValue({ status: 401 });
    await refreshSession();
    expect(pb.authStore.token).toBe("");
  });
});
