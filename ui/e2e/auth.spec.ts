import { test, expect, type BrowserContext, type Page } from "@playwright/test";
import { app } from "../src/config";
const entity = app.entities[0];
const id = "record000000001";
const key = app.name + ".reader.auth";
const token = (who: string, exp = 4102444800) =>
  `test.${Buffer.from(JSON.stringify({ exp, id: who })).toString("base64url")}.test`;
const auth = (who: string) => ({
  token: token(who),
  record: { id: who, collectionName: app.authCollection, name: who },
});
async function setup(context: BrowserContext) {
  await context.route("**/api/collections/*/auth-with-password", (route) =>
    route.fulfill({ json: auth(route.request().postDataJSON().identity) }),
  );
  await context.route("**/api/collections/*/auth-refresh", (route) => {
    const authorization = route.request().headers().authorization;
    return route.fulfill({
      json:
        authorization === token("second@example.com")
          ? auth("second@example.com")
          : auth("first@example.com"),
    });
  });
  await context.route("**/api/context/query", (route) => {
    const who =
      route.request().headers().authorization === token("second@example.com")
        ? "Second private record"
        : "First private record";
    const sql = route.request().postDataJSON().sql;
    const row = {
      id,
      ...Object.fromEntries(entity.title.map((field) => [field, who])),
    };
    const rows = sql.includes(`FROM "${entity.table}"`) ? [row] : [];
    return route.fulfill({
      json: {
        columns: Object.keys(row),
        rows: rows.map((r) => Object.values(r)),
        truncated: false,
      },
    });
  });
}
async function login(page: Page, who = "first@example.com") {
  await page.getByLabel("Email", { exact: true }).fill(who);
  await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}
test("independent tabs restore deep links and synchronize logout and account changes", async ({
  page,
  context,
}) => {
  await setup(context);
  const path = `/#/${entity.table}/${id}`;
  await page.goto(path);
  await login(page);
  await expect(page.locator("main h1")).toContainText("First private record");
  const other = await context.newPage();
  await other.goto(path);
  await expect(other.locator("main h1")).toContainText("First private record");
  await other.reload();
  await expect(other.locator("main h1")).toContainText("First private record");
  // Change account directly via the official SDK in one tab without a signed-out gap.
  await other.evaluate(async () => {
    const { signIn } = await import(/* @vite-ignore */ "/src/" + "api.ts");
    await signIn("second@example.com", "synthetic-password");
  });
  for (const tab of [page, other]) {
    await expect(tab.locator("main h1")).toContainText("Second private record");
    await expect(
      tab.getByText("First private record", { exact: true }),
    ).toHaveCount(0);
  }
  await other.getByRole("button", { name: "Sign out" }).click();
  for (const tab of [page, other]) {
    await expect(
      tab.getByRole("button", { name: "Sign in", exact: true }),
    ).toBeVisible();
    await expect(tab.locator("main")).toHaveCount(0);
    expect(
      await tab.evaluate((key) => localStorage.getItem(key), key),
    ).toBeNull();
    await expect(tab).toHaveURL(new RegExp(`/${entity.table}/${id}$`));
  }
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
});
test("old tab credentials and expired persistent tokens cannot restore a session", async ({
  page,
  context,
}) => {
  await setup(context);
  await page.addInitScript(
    ({ key, old, expired }) => {
      sessionStorage.setItem(key, JSON.stringify(old));
      localStorage.setItem(key, JSON.stringify(expired));
    },
    {
      key,
      old: auth("first@example.com"),
      expired: {
        ...auth("first@example.com"),
        token: token("first@example.com", 1),
      },
    },
  );
  await page.goto(`/#/${entity.table}/${id}`);
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate((key) => sessionStorage.getItem(key), key),
  ).toBeNull();
  expect(
    await page.evaluate((key) => localStorage.getItem(key), key),
  ).toBeNull();
});

test("same-account token renewal retains the reader and visible search results", async ({page, context}) => {
  await setup(context);
  await page.goto(`/#/${entity.table}/${id}`);
  await login(page);
  await expect(page.locator("main h1")).toContainText("First private record");
  await expect(page.locator(".results .result")).toHaveCount(1);
  await page.locator("main").evaluate(node => node.setAttribute("data-retained", "yes"));
  let renewals = 0;
  page.on("request", request => { if (request.url().endsWith("/auth-refresh")) renewals++; });
  await page.evaluate(async () => {
    const {pb} = await import(/* @vite-ignore */ "/src/" + "api.ts");
    pb.authStore.save(pb.authStore.token + "renewed", pb.authStore.record);
  });
  await expect(page.locator('main[data-retained="yes"]')).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  // Wait through another browser task after the synchronous auth listeners.
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 50)));
  expect(renewals).toBe(0);
  await expect(page.locator(".results .result")).toHaveCount(1);
});

test("tabs adopt distinct slow renewal tokens without a refresh loop", async ({page, context}) => {
  await setup(context);
  let renewals = 0;
  await context.route("**/api/collections/*/auth-refresh", async route => {
    const number = ++renewals;
    await new Promise(resolve => setTimeout(resolve, 100));
    const result = auth("first@example.com");
    await route.fulfill({json: {...result, token: result.token + number}});
  });
  await page.goto(`/#/${entity.table}/${id}`);
  await login(page);
  await expect(page.locator("main h1")).toBeVisible();
  const other = await context.newPage();
  await other.goto(`/#/${entity.table}/${id}`);
  await expect.poll(() => renewals).toBe(1);
  await expect.poll(() => page.evaluate(key => JSON.parse(localStorage.getItem(key) || "{}").token, key)).toBe(token("first@example.com") + "1");
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await other.evaluate(() => window.dispatchEvent(new Event("focus")));
  await new Promise(resolve => setTimeout(resolve, 350));
  expect(renewals).toBe(1);
  await expect(page.locator("main h1")).toBeVisible();
  await expect(other.locator("main h1")).toBeVisible();
});
