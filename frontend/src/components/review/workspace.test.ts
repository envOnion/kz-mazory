import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import WorkspaceView from "../WorkspaceView.vue";
import { currentUser } from "../../composables/session";
import type { Candidate } from "../../types/platform";
const mocks = vi.hoisted(() => ({ api: vi.fn(), post: vi.fn() }));
vi.mock("../../composables/api", () => ({
  api: mocks.api,
  post: mocks.post,
  pollOperation: vi.fn(),
  download: vi.fn(),
}));
const fact: Candidate = {
  id: 1,
  project_id: 1,
  project_name: "Школа",
  team_id: 1,
  manager_id: null,
  fact_type: "project",
  proposed_changes: {
    object_name: "Школа",
    contract_amount: "1000",
    currency: "KZT",
  },
  status: "pending",
  base_version: 1,
  current_version: 1,
  confidence: 0.9,
  uncertainties: [],
  evidence: [
    { id: 1, quote: "1000", source_id: 1, source_url: "/api/messages/1/" },
  ],
  review_reason: "",
  created_at: "2026-10-01T12:00:00Z",
  crm_resolution: {
    state: "not_found",
    revision: 1,
    checked_at: null,
    error_code: "",
    options: [],
  },
};
const directory = { teams: [], profiles: [], projects: [] };
async function screen(item = fact, finance = true) {
  currentUser.value = {
    id: 1,
    name: "Тест",
    phone: "test",
    username: "test",
    roles: finance ? ["finance", "team_lead"] : ["team_lead"],
  };
  mocks.api.mockImplementation(async (url: string) =>
    url === "/directory/"
      ? directory
      : url.startsWith("/candidates/")
        ? { results: [item], count: 51, next: "/next" }
        : { results: [], count: 0, next: null },
  );
  const wrapper = mount(WorkspaceView);
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "Проверка фактов")!
    .trigger("click");
  await flushPromises();
  return wrapper;
}
afterEach(() => {
  currentUser.value = null;
  vi.clearAllMocks();
});
describe("Review workflow", () => {
  it("preserves financial permissions and explains disabled approval", async () => {
    const wrapper = await screen(fact, false);
    expect(
      wrapper.findAll("button").find((b) => b.text() === "Подтвердить факт"),
    ).toBeUndefined();
    expect(wrapper.text()).not.toContain("Исправить значения");
    wrapper.unmount();
  });
  it("posts corrections with the base version and the reason from the form", async () => {
    mocks.post.mockResolvedValue({});
    const wrapper = await screen();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "Исправить значения")!
      .trigger("click");
    const form = wrapper.get("form");
    await form.findAll("input")[2]!.setValue("1500");
    await form.get("textarea[placeholder]").setValue("Уточнение");
    await form.trigger("submit");
    await flushPromises();
    expect(mocks.post).toHaveBeenCalledWith("/candidates/1/review/", {
      action: "approve",
      base_version: 1,
      reason: "Уточнение",
      changes: { contract_amount: "1500" },
    });
    wrapper.unmount();
  });
  it("blocks direct approval of cumulative totals and stale project data", async () => {
    const wrapper = await screen({
      ...fact,
      fact_type: "payment",
      proposed_changes: { amount: "1000", payment_kind: "cumulative" },
    });
    expect(
      wrapper
        .findAll("button")
        .find((b) => b.text() === "Подтвердить факт")!
        .attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.text()).toContain("накопленный итог");
    wrapper.unmount();
    const stale = await screen({ ...fact, current_version: 2 });
    expect(stale.text()).toContain("Данные проекта изменились");
    expect(
      stale
        .findAll("button")
        .find((b) => b.text() === "Подтвердить факт")!
        .attributes("disabled"),
    ).toBeDefined();
    stale.unmount();
  });
  it("loads subsequent result pages explicitly", async () => {
    const wrapper = await screen();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "Следующая")!
      .trigger("click");
    await flushPromises();
    expect(mocks.api).toHaveBeenCalledWith(
      "/candidates/?status=pending&page=2",
    );
    wrapper.unmount();
  });
});

describe("General commitments and project discovery", () => {
  it("allows review of a general team commitment with an empty project directory", async () => {
    const wrapper = await screen({
      ...fact,
      fact_type: "commitment",
      project_id: null,
      project_name: "",
      base_version: 0,
      current_version: 0,
      proposed_changes: {
        commitment_text: "Отправить список объектов",
        commitment_status: "pending",
        responsible_name: "Жанат",
      },
    });
    expect(wrapper.text()).not.toContain(
      "Сначала выберите существующий подтверждённый проект",
    );
    const approve = wrapper
      .findAll("button")
      .find((b) => b.text() === "Подтвердить факт")!;
    expect(approve.attributes("disabled")).toBeUndefined();
    expect(wrapper.text()).toContain(
      "общие обязательства команды можно подтверждать без проекта",
    );
    wrapper.unmount();
  });
  it("refreshes the CRM directory when available projects are missing", async () => {
    const wrapper = await screen();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "Обновить справочник")!
      .trigger("click");
    await flushPromises();
    expect(mocks.api).toHaveBeenCalledWith(
      "/directory/",
    );
    wrapper.unmount();
  });
});

describe('Project workspace', () => {
  async function projectsScreen() {
    currentUser.value = { id: 1, name: 'Тест', phone: 'test', username: 'test', roles: ['finance'] };
    mocks.api.mockImplementation(async (url: string) => {
      if (url === '/directory/') return directory;
      if (url.startsWith('/projects/?')) return {
        count: 51, next: url.includes('page=2') ? null : '/api/projects/?page=2',
        group_counts: { with_data: 1, without_data: 50 },
        results: [{ id: 870, name: 'ЦТП 343', status: 'В исполнении', version: 1,
          contract_formatted: '0.00 ₸', paid_formatted: '117 000 000.00 ₸', due_formatted: '-117 000 000.00 ₸',
          contract_known: false, payments_known: true, balance_known: false,
          missing_data_reasons: [{ code: 'contract_missing', message: 'Сумма договора не зарегистрирована.' }], review_available: false }],
      };
      return { results: [], count: 0, next: null };
    });
    const wrapper = mount(WorkspaceView);
    await flushPromises();
    return wrapper;
  }
  it('shows known payments and explains missing contract without a false negative balance', async () => {
    const wrapper = await projectsScreen();
    expect(wrapper.text()).toContain('Поступило: 117 000 000.00 ₸');
    expect(wrapper.text()).toContain('Договор: Сумма не указана');
    expect(wrapper.text()).toContain('Остаток: Не определён');
    expect(wrapper.text()).not.toContain('-117 000 000');
    expect(wrapper.text()).toContain('Сумма договора не зарегистрирована.');
    wrapper.unmount();
  });
  it('requests subsequent pages and resets page on a list/filter change', async () => {
    const wrapper = await projectsScreen();
    await wrapper.get('nav[aria-label="Страницы проектов"]').findAll('button')[1]!.trigger('click');
    await flushPromises();
    expect(mocks.api).toHaveBeenCalledWith('/projects/?page=2&group=with_data&completeness=all');
    await wrapper.get('nav[aria-label="Данные проектов"]').findAll('button')[1]!.trigger('click');
    await flushPromises();
    expect(mocks.api).toHaveBeenCalledWith('/projects/?page=1&group=without_data&completeness=all');
    await wrapper.get('input[placeholder="Название"]').setValue('Top Build');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(mocks.api).toHaveBeenCalledWith('/projects/?page=1&group=without_data&completeness=all&search=Top+Build');
    wrapper.unmount();
  });
});
