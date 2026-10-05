"use strict";
(() => {
  let theme = "dark";
  try {
    const saved =
      document.cookie
        .split("; ")
        .find((item) => item.startsWith("kotonoha-theme="))
        ?.split("=")[1] || localStorage.getItem("kotonoha-theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch {}
  document.documentElement.dataset.theme = theme;
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    function render() {
      const dark = document.documentElement.dataset.theme === "dark";
      button.textContent = dark ? "라이트 모드" : "다크 모드";
      button.setAttribute("aria-pressed", String(dark));
      button.setAttribute(
        "aria-label",
        dark ? "라이트 모드로 전환" : "다크 모드로 전환",
      );
    }
    button.addEventListener("click", () => {
      theme =
        document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = theme;
      try {
        document.cookie = `kotonoha-theme=${theme}; Path=/; Max-Age=31536000; SameSite=Strict`;
        localStorage.setItem("kotonoha-theme", theme);
      } catch {}
      render();
    });
    render();
  });
})();

("use strict");
const $ = (id) => document.getElementById(id);
let state = null,
  renderedRevision = -1,
  selected = new Set(),
  latestPlan = null,
  candidateShown = false,
  toastTimer,
  pollBusy = false,
  renderedModelKey = "",
  renderedDictionaryKey = "",
  renderedRecordKey = "",
  renderedRoot = "",
  outputDirty = false;
let correctionDraft = [];
let pollTimer,
  stopped = false;
const tokenKey = "kotonoha-session";
const launchToken = new URLSearchParams(location.hash.slice(1)).get("token");
if (launchToken) {
  sessionStorage.setItem(tokenKey, launchToken);
  history.replaceState(null, "", location.pathname);
}
const sessionToken = sessionStorage.getItem(tokenKey);

async function request(path, payload) {
  const headers = { Authorization: "Bearer " + sessionToken };
  const options = { headers, cache: "no-store", credentials: "omit" };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!result.ok) throw new Error(result.error);
  return result.data;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6500);
}
async function invoke(action, payload = {}) {
  return request("/api/invoke", { action, payload });
}
async function act(action, payload = {}) {
  try {
    const result = await invoke(action, payload);
    await poll();
    return result;
  } catch (error) {
    toast(error.message);
    return null;
  }
}
function button(text, callback, disabled = false) {
  const element = document.createElement("button");
  element.textContent = text;
  element.disabled = disabled;
  element.addEventListener("click", callback);
  return element;
}
function cell(row, text) {
  const td = document.createElement("td");
  td.textContent = text;
  row.append(td);
  return td;
}
function close(id) {
  $(id).close();
}
function open(id) {
  if (!$(id).open) $(id).showModal();
}
function renderRows() {
  const body = $("titles").tBodies[0];
  body.replaceChildren();
  const query = $("search").value.trim().toLowerCase();
  const rows = state.rows.filter((row) =>
    (row.source + row.translation).toLowerCase().includes(query),
  );
  for (const row of rows) {
    const tr = document.createElement("tr");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = selected.has(row.id);
    check.setAttribute("aria-label", `${row.source} 선택`);
    check.addEventListener("change", () => {
      check.checked ? selected.add(row.id) : selected.delete(row.id);
    });
    cell(tr, "").append(check);
    cell(tr, row.source);
    const input = document.createElement("textarea");
    input.value = row.translation;
    input.disabled = state.busy;
    input.setAttribute("aria-label", `${row.source} 번역 수정`);
    input.spellcheck = false;
    input.addEventListener("change", async () => {
      if (input.value.trim() !== row.translation)
        await act("edit", { id: row.id, text: input.value });
    });
    cell(tr, "").append(input);
    const badge = document.createElement("span");
    badge.className =
      "badge" +
      (row.status === "확인 필요" ? " warning" : row.manual ? " manual" : "");
    badge.textContent = row.status;
    cell(tr, "").append(badge);
    cell(tr, String(row.count));
    body.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = cell(
      tr,
      state.root
        ? "표시할 일본어 제목이 없습니다. 결과 폴더 이름을 정해 내용물을 정리할 수 있습니다."
        : "대상 폴더를 선택하세요.",
    );
    td.colSpan = 5;
    td.className = "empty";
    body.append(tr);
  }
  $("row-count").textContent = `제목 ${state.rows.length}개`;
}
function modelLabel(name) {
  return name ? name.split(":").pop() : "미선택";
}
function renderModels() {
  const key = JSON.stringify([state.models, state.busy]);
  if (key === renderedModelKey) return;
  renderedModelKey = key;
  const m = state.models;
  $("models-error").textContent = m.error;
  $("ollama-state").textContent = m.available
    ? "실행 중"
    : m.ollama_installed
      ? "설치됨 · 연결 확인 필요"
      : "설치되지 않음";
  $("ollama-install").textContent = m.ollama_installed ? "설치됨" : "설치";
  $("ollama-install").disabled = m.ollama_installed || state.busy;
  const body = $("model-table").tBodies[0];
  body.replaceChildren();
  for (const model of m.items) {
    const tr = document.createElement("tr");
    cell(tr, model.quant);
    cell(tr, `${(model.bytes / 1e9).toFixed(1)} GB`);
    cell(tr, `${model.vram_gb} GB 이상`);
    cell(tr, model.installed ? "✓" : "");
    cell(tr, m.default === model.model ? "✓" : "");
    const actions = cell(tr, "");
    if (model.installed) {
      actions.append(
        button(
          "사용",
          async () => {
            if (m.default === model.model) return;
            if (
              confirm("다운로드 없이 이 모델을 기본 모델로 사용하시겠습니까?")
            )
              await act("default_model", { model: model.model });
          },
          state.busy,
        ),
      );
      actions.append(
        button(
          "조회",
          () => act("model_details", { model: model.model }),
          state.busy,
        ),
      );
      actions.append(
        button(
          "삭제",
          async () => {
            if (confirm(`${model.quant} 모델을 삭제하시겠습니까?`))
              await act("delete_model", { model: model.model });
          },
          state.busy,
        ),
      );
    } else
      actions.append(
        button(
          "다운로드",
          async () => {
            if (
              confirm(
                `${model.quant} 모델을 다운로드하고 기본 모델로 설정하시겠습니까?`,
              )
            )
              await act("install_model", { model: model.model });
          },
          state.busy,
        ),
      );
    body.append(tr);
  }
}
function renderPreferences() {
  const key = JSON.stringify([
    state.preferences,
    state.root,
    state.rows.map((row) => [row.id, row.source]),
    state.busy,
  ]);
  if (key === renderedDictionaryKey) return;
  renderedDictionaryKey = key;
  const container = $("preferences");
  container.replaceChildren();
  for (const [source, value] of Object.entries(state.preferences)) {
    const item = document.createElement("div");
    item.className = "preference";
    const label = document.createElement("span");
    label.textContent = `${source} → ${value}`;
    item.append(
      label,
      button("삭제", () => act("delete_preference", { source }), state.busy),
    );
    container.append(item);
  }
  if (!Object.keys(state.preferences).length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "등록한 표현이 없습니다.";
    container.append(empty);
  }
  const dropdown = $("pref-row");
  const current = dropdown.value;
  dropdown.replaceChildren();
  for (const row of state.rows) {
    const option = document.createElement("option");
    option.value = String(row.id);
    option.textContent = row.source;
    dropdown.append(option);
  }
  if (current) dropdown.value = current;
  const original = state.rows.find(
    (row) => String(row.id) === dropdown.value,
  )?.source;
  if (
    !$("pref-original").textContent ||
    dropdown.value !== current ||
    (original && $("pref-original").textContent !== original)
  )
    updateOriginal();
  $("pref-row").disabled = state.busy || !state.rows.length;
  $("pref-desired").disabled = state.busy;
  $("pref-save").disabled = state.busy || !state.rows.length;
}
function updateOriginal() {
  const row = state.rows.find(
    (item) => String(item.id) === $("pref-row").value,
  );
  $("pref-original").textContent = row
    ? row.source
    : "폴더를 선택한 뒤 원문 표현을 고를 수 있습니다.";
  $("pref-source").value = "";
}
function renderRecords() {
  const key = JSON.stringify([state.records, state.busy]);
  if (key === renderedRecordKey) return;
  renderedRecordKey = key;
  const container = $("records");
  container.replaceChildren();
  const labels = {
    completed: "적용 완료",
    partial_failure: "일부 적용·이동 보류",
    in_progress: "적용 중단",
    undo_partial_failure: "복구 중단",
    undo_in_progress: "복구 중단",
    undone: "복구 완료",
  };
  for (const record of state.records) {
    const item = document.createElement("div");
    item.className = "record";
    const strong = document.createElement("strong");
    strong.textContent = labels[record.status] || record.status;
    const root = document.createElement("p");
    root.textContent = record.root;
    const output = document.createElement("p");
    output.textContent = `결과: ${record.output} · 적용 ${record.completed}/${record.total}개`;
    item.append(strong, root, output);
    if (!record.status.startsWith("undo") && record.status !== "completed")
      item.append(
        button(
          "남은 변경 계속",
          async () => {
            if (confirm("이 기록의 남은 변경을 계속하시겠습니까?")) {
              close("records-dialog");
              await act("resume", { id: record.id });
            }
          },
          state.busy,
        ),
      );
    if (record.status !== "undone")
      item.append(
        button(
          "원래 위치·이름 복구",
          async () => {
            if (
              confirm("이 기록의 변경을 원래 위치와 이름으로 복구하시겠습니까?")
            ) {
              close("records-dialog");
              await act("undo", { id: record.id });
            }
          },
          state.busy,
        ),
      );
    if (record.status === "completed")
      item.append(
        button(
          "복구 기록 정리",
          async () => {
            if (
              confirm(
                "복구 기록을 삭제하면 이 작업을 자동 복구할 수 없습니다. 삭제하시겠습니까?",
              )
            )
              await act("delete_record", { id: record.id });
          },
          state.busy,
        ),
      );
    container.append(item);
  }
  if (!state.records.length)
    container.textContent = "저장된 작업 기록이 없습니다.";
}
function renderCandidate() {
  const body = $("comparison").tBodies[0];
  body.replaceChildren();
  for (const row of state.candidate) {
    const tr = document.createElement("tr");
    cell(tr, row.source);
    cell(tr, row.current);
    cell(tr, row.candidate);
    body.append(tr);
  }
  open("candidate-dialog");
  candidateShown = true;
}
function render() {
  const s = state;
  if (renderedRoot !== s.root) {
    selected.clear();
    $("select-all").checked = false;
    outputDirty = false;
    renderedRoot = s.root;
    renderedRevision = -1;
  }
  $("current-model").textContent = `SuperGemma · ${modelLabel(s.model)}`;
  $("phase").textContent = s.job.label;
  $("dot").className =
    "status-dot " +
    (s.busy ? "running" : ["failed", "partial"].includes(s.job.status) ? "failed" : "");
  $("progress-count").textContent = s.job.total
    ? `${s.job.done} / ${s.job.total}${s.job.unit === "percent" ? "%" : "개"}`
    : "";
  $("elapsed").textContent =
    s.busy && s.job.started
      ? `경과 ${Math.floor(Date.now() / 1000 - s.job.started)}초` +
        (s.job.last_response
          ? ` · 마지막 응답 ${Math.max(0, Math.floor(Date.now() / 1000 - s.job.last_response))}초 전`
          : "")
      : "";
  const progress = $("progress");
  if (s.busy && !s.job.total) progress.removeAttribute("value");
  else progress.value = s.job.total ? (s.job.done * 100) / s.job.total : 0;
  $("message").textContent = s.message || s.job.error || "";
  $("memory").textContent = s.unload;
  $("cancel").hidden = !s.busy;
  $("cancel").textContent =
    s.job.status === "cancelling" ? "중단 처리 중" : "작업 중단";
  $("cancel").disabled = s.job.status === "cancelling";
  for (const id of [
    "pick",
    "scan",
    "translate",
    "retry",
    "restore",
    "correction-open",
    "preview",
    "strength",
    "output-name",
    "models-refresh",
    "ollama-update",
  ]) {
    $(id).disabled =
      s.busy ||
      ([
        "translate",
        "retry",
        "restore",
        "correction-open",
        "preview",
        "strength",
        "output-name",
      ].includes(id) &&
        !s.root);
  }
  if (document.activeElement !== $("path")) $("path").value = s.root;
  if (document.activeElement !== $("strength"))
    $("strength").value = s.strength;
  $("strength-value").textContent = `${$("strength").value} / 10`;
  if (
    !outputDirty &&
    document.activeElement !== $("output-name") &&
    renderedRevision !== s.revision
  )
    $("output-name").value = s.output_name;
  $("output-path").textContent = s.root
    ? s.root + String.fromCharCode(92) + $("output-name").value
    : "원본 폴더 안에 새 폴더를 만듭니다.";
  if (renderedRevision !== s.revision) {
    renderRows();
    renderedRevision = s.revision;
  }
  for (const input of $("titles").querySelectorAll("textarea"))
    input.disabled = s.busy;
  if ($("models-dialog").open) renderModels();
  if ($("dictionary-dialog").open) renderPreferences();
  if ($("records-dialog").open) renderRecords();
  if (s.candidate.length && !s.busy && !candidateShown) renderCandidate();
  if (!s.candidate.length) candidateShown = false;
}
async function poll() {
  if (pollBusy || stopped) return;
  pollBusy = true;
  try {
    const snapshot = await request("/api/snapshot");
    if (stopped) return;
    state = snapshot;
    render();
  } catch (error) {
    toast(error.message);
  } finally {
    pollBusy = false;
  }
}
function correctionRows() {
  const container = $("corrections");
  container.replaceChildren();
  correctionDraft.forEach((entry, index) => {
    const row = document.createElement("div");
    row.className = "correction";
    const old = document.createElement("input"),
      desired = document.createElement("input");
    old.placeholder = "바꿀 표현";
    old.value = entry.current_expression;
    desired.placeholder = "원하는 표현";
    desired.value = entry.desired_expression;
    old.addEventListener("input", () => (entry.current_expression = old.value));
    desired.addEventListener(
      "input",
      () => (entry.desired_expression = desired.value),
    );
    const arrow = document.createElement("span");
    arrow.textContent = "→";
    row.append(
      old,
      arrow,
      desired,
      button("삭제", () => {
        correctionDraft.splice(index, 1);
        correctionRows();
      }),
    );
    container.append(row);
  });
}
async function retry(corrections = []) {
  if (
    !selected.size &&
    !confirm("직접 수정한 제목을 제외한 전체 제목을 다시 번역하시겠습니까?")
  )
    return;
  candidateShown = false;
  await act("retry", { ids: [...selected], corrections });
}
async function boot() {
  if (!sessionToken) {
    $("message").textContent =
      "Kotonoha.exe를 실행하면 접속 가능한 화면이 열립니다.";
    for (const el of document.querySelectorAll("button, input"))
      el.disabled = true;
    return;
  }
  await poll();
  pollTimer = setInterval(poll, 500);
  $("shutdown").onclick = async () => {
    if (
      state?.busy &&
      !confirm("현재 작업을 중단하고 프로그램을 종료하시겠습니까?")
    )
      return;
    try {
      await invoke("shutdown");
      stopped = true;
      clearInterval(pollTimer);
      $("phase").textContent = "종료";
      $("message").textContent = "종료했습니다. 이 탭을 닫으세요.";
      for (const el of document.querySelectorAll("button, input, textarea"))
        el.disabled = true;
    } catch (error) {
      toast(error.message);
    }
  };
  $("pick").onclick = () => act("pick_folder");
  $("scan").onclick = () => act("scan", { path: $("path").value });
  $("path").onkeydown = (e) => {
    if (e.key === "Enter") $("scan").click();
  };
  $("translate").onclick = () => act("translate");
  $("retry").onclick = () => retry();
  $("restore").onclick = () => act("restore");
  $("strength").oninput = () =>
    ($("strength-value").textContent = `${$("strength").value} / 10`);
  $("strength").onchange = () =>
    act("strength", { value: Number($("strength").value) });
  $("search").oninput = renderRows;
  $("select-all").onchange = () => {
    selected = $("select-all").checked
      ? new Set(state.rows.map((row) => row.id))
      : new Set();
    renderRows();
  };
  $("cancel").onclick = () => act("cancel");
  $("models-open").onclick = async () => {
    open("models-dialog");
    renderModels();
    if (!state.busy) await act("models");
  };
  $("dictionary-open").onclick = () => {
    open("dictionary-dialog");
    renderPreferences();
  };
  $("models-refresh").onclick = () => act("models");
  $("ollama-install").onclick = async () => {
    if (confirm("Ollama를 설치하시겠습니까?")) await act("setup_ollama");
  };
  $("ollama-update").onclick = async () => {
    if (confirm("Ollama를 업데이트하시겠습니까?"))
      await act("setup_ollama", { update: true });
  };
  $("records-open").onclick = async () => {
    await act("records");
    open("records-dialog");
    renderRecords();
  };
  $("pick-log").onclick = () => act("pick_log");
  $("correction-open").onclick = () => {
    if (!correctionDraft.length)
      correctionDraft.push({ current_expression: "", desired_expression: "" });
    correctionRows();
    open("correction-dialog");
  };
  $("correction-add").onclick = () => {
    correctionDraft.push({ current_expression: "", desired_expression: "" });
    correctionRows();
  };
  $("correction-run").onclick = async () => {
    if (
      correctionDraft.some(
        (item) =>
          !item.current_expression.trim() || !item.desired_expression.trim(),
      )
    )
      return toast("바꿀 표현과 원하는 표현을 입력하세요.");
    close("correction-dialog");
    candidateShown = false;
    await act("replace", { ids: [...selected], corrections: correctionDraft });
  };
  $("candidate-keep").onclick = async () => {
    await act("adopt", { accept: false });
    close("candidate-dialog");
  };
  $("candidate-adopt").onclick = async () => {
    await act("adopt", { accept: true });
    close("candidate-dialog");
  };
  $("preview").onclick = async () => {
    latestPlan = await act("preview", { output_name: $("output-name").value });
    if (!latestPlan) return;
    $("preview-summary").textContent =
      `${latestPlan.reapply ? "기존 결과에 수정한 이름을 다시 적용합니다.\n" : ""}결과 폴더: ${latestPlan.output}\n변경·이동 ${latestPlan.changes.length}개` +
      (latestPlan.incomplete
        ? ` · 미완료 제목 ${latestPlan.incomplete}개는 이름을 유지합니다.`
        : "");
    const body = $("changes").tBodies[0];
    body.replaceChildren();
    for (const change of latestPlan.changes) {
      const tr = document.createElement("tr");
      cell(tr, change.source);
      cell(tr, change.target);
      body.append(tr);
    }
    $("apply").textContent = latestPlan.reapply
      ? "수정한 이름으로 다시 적용"
      : "확인한 이름으로 적용";
    open("preview-dialog");
  };
  $("apply").onclick = async () => {
    if (!latestPlan) return;
    const plan = latestPlan;
    latestPlan = null;
    close("preview-dialog");
    await act("apply", { plan_id: plan.plan_id, revision: plan.revision });
  };
  $("open-output").onclick = () => act("open_output");
  $("output-name").oninput = () => {
    outputDirty = true;
    $("output-path").textContent =
      state.root + String.fromCharCode(92) + $("output-name").value;
  };
  $("pref-row").onchange = updateOriginal;
  $("pref-original").onmouseup = () => {
    const selection = window.getSelection();
    if (selection && $("pref-original").contains(selection.anchorNode))
      $("pref-source").value = selection.toString().trim();
  };
  $("pref-save").onclick = async () => {
    const result = await act("preference", {
      source: $("pref-source").value,
      desired: $("pref-desired").value,
    });
    if (result === null) return;
    $("pref-source").value = "";
    $("pref-desired").value = "";
    window.getSelection()?.removeAllRanges();
  };
  for (const el of document.querySelectorAll("[data-close]"))
    el.onclick = () => close(el.dataset.close);
  $("candidate-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    $("candidate-keep").click();
  });
}
window.addEventListener("DOMContentLoaded", boot);
