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

it("renewal keeps the same identity generation and accepts its pending reads", async () => {
  const { sessionEpoch } = await import("./api");
  pb.authStore.save(token, record);
  const started = sessionEpoch();
  vi.spyOn(pb, "send").mockImplementation(async () => {
    pb.authStore.save(token + "renewed", record);
    return { columns: ["id"], rows: [["same-account"]], truncated: false };
  });
  await expect(query("SELECT id FROM records")).resolves.toEqual([{id: "same-account"}]);
  expect(sessionEpoch()).toBe(started);
});

it("a new account refresh never waits for the previous account's pending renewal", async () => {
  vi.spyOn(Date, "now").mockReturnValue(2000001800000);
  let finishFirst: (value: unknown) => void = () => {};
  const request = vi.spyOn(PocketBase.prototype, "send")
    .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }))
    .mockResolvedValueOnce({ token: token + "second-renewed", record: {...record, id: "second"} });
  pb.authStore.save(token, record);
  const first = refreshSession();
  pb.authStore.save(token + "second", {...record, id: "second"});
  await refreshSession();
  expect(request).toHaveBeenCalledTimes(2);
  expect(pb.authStore.record?.id).toBe("second");
  finishFirst({token: token + "old-renewed", record});
  await first;
  expect(pb.authStore.record?.id).toBe("second");
  expect(pb.authStore.token).toBe(token + "second-renewed");
});

it("adopts another tab's same-account renewal without renewing it back", async () => {
  vi.spyOn(Date, "now").mockReturnValue(2000002400000);
  pb.authStore.save(token, record);
  pb.authStore.save(token + "external-renewal", record);
  const request = vi.spyOn(PocketBase.prototype, "send");
  await refreshSession();
  expect(request).not.toHaveBeenCalled();
});
