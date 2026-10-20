(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const params = new URLSearchParams(location.search);
  const editId = params.get("edit");
  const copyId = params.get("copy");
  const mode = editId ? "edit" : copyId ? "copy" : "create";

  const form = document.getElementById("form");
  const errorBox = document.getElementById("error-box");
  const loading = document.getElementById("loading");
  const doneBox = document.getElementById("done");

  const state = {
    config: null,
    submitting: false,
    values: {
      department: "",
      company: "",
      vehicle_type: "",
      cargo_type: "",
      crop: "",
      size_type: "",
      date_type: "single",
      load_cities: [],
      unload_cities: [],
      distribution: {},
    },
  };

  const chipBoxes = {};
  const pickers = {};

  // ---------- helpers ----------

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function field(name) {
    return form.elements.namedItem(name);
  }

  function num(value) {
    const n = parseFloat(String(value == null ? "" : value).replace(/\s/g, "").replace(",", "."));
    return Number.isFinite(n) ? n : null;
  }

  function fmt(n) {
    return Number.isInteger(n) ? String(n) : String(Math.round(n * 1000) / 1000);
  }

  function api(path, options) {
    options = options || {};
    const headers = { "X-Telegram-Init-Data": tg ? tg.initData : "" };
    if (options.body) headers["Content-Type"] = "application/json";
    return fetch(path, Object.assign({}, options, { headers: headers })).then(async function (response) {
      let data;
      try {
        data = await response.json();
      } catch (e) {
        data = { ok: false, error: "Сервер повернув некоректну відповідь (" + response.status + ")" };
      }
      if (!response.ok || !data.ok) {
        const error = new Error(data.error || "Помилка " + response.status);
        error.data = data;
        throw error;
      }
      return data;
    });
  }

  function haptic(type) {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(type);
  }

  function showError(message, data) {
    errorBox.hidden = false;
    errorBox.textContent = message;
    if (data && data.can_copy && editId) {
      const button = el("button", "primary");
      button.type = "button";
      button.textContent = "📋 Створити нову заявку на основі цієї";
      button.addEventListener("click", function () {
        location.search = "?copy=" + encodeURIComponent(editId);
      });
      errorBox.appendChild(button);
    }
    errorBox.scrollIntoView({ behavior: "smooth", block: "start" });
    haptic("error");
  }

  function clearError() {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  // ---------- chips ----------

  function renderChips(name, options) {
    const box = form.querySelector('[data-chips="' + name + '"]');
    box.innerHTML = "";
    options.forEach(function (option) {
      const chip = el("button", "chip");
      chip.type = "button";
      chip.textContent = option;
      chip.dataset.value = option;
      chip.addEventListener("click", function () {
        setValue(name, option);
      });
      box.appendChild(chip);
    });
    chipBoxes[name] = box;
    syncChips(name);
  }

  function syncChips(name) {
    const box = chipBoxes[name];
    if (!box) return;
    box.querySelectorAll(".chip").forEach(function (chip) {
      chip.classList.toggle("active", chip.dataset.value === state.values[name]);
    });
  }

  function setValue(name, value) {
    state.values[name] = value;
    if (chipBoxes[name]) chipBoxes[name].classList.remove("invalid");
    syncChips(name);
    updateVisibility();
  }

  // ---------- segmented ----------

  function initSegmented() {
    const box = form.querySelector('[data-segmented="date_type"]');
    box.querySelectorAll("button").forEach(function (button) {
      button.addEventListener("click", function () {
        state.values.date_type = button.dataset.value;
        syncSegmented();
        updateVisibility();
      });
    });
    syncSegmented();
  }

  function syncSegmented() {
    form.querySelectorAll('[data-segmented="date_type"] button').forEach(function (button) {
      button.classList.toggle("active", button.dataset.value === state.values.date_type);
    });
  }

  // ---------- city picker ----------

  function createCityPicker(name) {
    const root = form.querySelector('[data-city="' + name + '"]');
    root.innerHTML = "";
    const selected = el("div", "selected");
    const input = el("input");
    input.type = "search";
    input.placeholder = "Почніть вводити назву…";
    input.autocomplete = "off";
    const results = el("ul", "results");
    root.append(selected, input, results);

    let timer = null;
    let sequence = 0;

    function render() {
      selected.innerHTML = "";
      state.values[name].forEach(function (city, index) {
        const tag = el("span", "city-tag");
        const text = el("span");
        text.textContent = city;
        text.title = city;
        const remove = el("button");
        remove.type = "button";
        remove.textContent = "✕";
        remove.setAttribute("aria-label", "Прибрати " + city);
        remove.addEventListener("click", function () {
          state.values[name].splice(index, 1);
          render();
          onCitiesChanged();
        });
        tag.append(text, remove);
        selected.appendChild(tag);
      });
    }

    function add(city) {
      city = (city || "").trim();
      if (!city) return;
      if (state.values[name].indexOf(city) === -1) state.values[name].push(city);
      input.value = "";
      results.innerHTML = "";
      root.classList.remove("invalid");
      input.classList.remove("invalid");
      render();
      onCitiesChanged();
    }

    function showResults(query, items) {
      results.innerHTML = "";
      if (!items.length) {
        const empty = el("li", "empty");
        empty.textContent = "Нічого не знайдено в довіднику Нової Пошти";
        results.appendChild(empty);
      }
      items.forEach(function (item) {
        const li = el("li");
        li.textContent = item;
        li.addEventListener("click", function () { add(item); });
        results.appendChild(li);
      });
      const manual = el("li", "manual");
      manual.textContent = "➕ Додати «" + query + "» як є";
      manual.addEventListener("click", function () { add(query); });
      results.appendChild(manual);
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      const query = input.value.trim();
      if (query.length < 2) {
        results.innerHTML = "";
        return;
      }
      timer = setTimeout(async function () {
        const current = ++sequence;
        let items = [];
        try {
          items = (await api("/api/cities?q=" + encodeURIComponent(query))).items || [];
        } catch (e) {
          items = [];
        }
        if (current === sequence) showResults(query, items);
      }, 350);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const first = results.querySelector("li:not(.manual):not(.empty)");
      if (first) first.click();
      else if (input.value.trim()) add(input.value);
    });

    pickers[name] = { render: render, input: input, root: root };
    render();
  }

  function onCitiesChanged() {
    renderDistribution();
    updateVisibility();
  }

  // ---------- distribution ----------

  function renderDistribution() {
    const cities = state.values.unload_cities;
    const box = document.getElementById("distribution");
    document.getElementById("distribution-field").hidden = cities.length < 2;
    Object.keys(state.values.distribution).forEach(function (city) {
      if (cities.indexOf(city) === -1) delete state.values.distribution[city];
    });
    box.innerHTML = "";
    if (cities.length < 2) return;

    cities.forEach(function (city) {
      const row = el("div", "dist-row");
      const label = el("span");
      label.textContent = city;
      label.title = city;
      const input = el("input");
      input.inputMode = "decimal";
      input.placeholder = "0";
      input.value = state.values.distribution[city] != null ? state.values.distribution[city] : "";
      input.addEventListener("input", function () {
        state.values.distribution[city] = input.value;
        input.classList.remove("invalid");
        updateDistributionSum();
      });
      row.append(label, input);
      box.appendChild(row);
    });
    updateDistributionSum();
  }

  function distributionSum() {
    return state.values.unload_cities.reduce(function (sum, city) {
      return sum + (num(state.values.distribution[city]) || 0);
    }, 0);
  }

  function updateDistributionSum() {
    const hint = document.getElementById("distribution-sum");
    if (state.values.unload_cities.length < 2) return;
    const total = num(field("volume").value);
    const sum = distributionSum();
    const ok = total != null && Math.abs(sum - total) < 1e-6;
    hint.textContent = "Розподілено " + fmt(sum) + " з " + (total == null ? "—" : fmt(total)) + (ok ? " ✓" : "");
    hint.classList.toggle("bad", !ok);
  }

  // ---------- visibility rules (як у чат-боті) ----------

  function updateVisibility() {
    const cfg = state.config;
    const v = state.values;
    const cargoBase = v.cargo_type;
    const isLiquid = cfg.liquid_cargo.indexOf(cargoBase) !== -1;
    const isCrop = cfg.crop_cargo_types.indexOf(cargoBase) !== -1;

    field("company_custom").hidden = v.company !== "Інше";
    field("vehicle_type_custom").hidden = v.vehicle_type !== "Інше";
    document.getElementById("crop-field").hidden = !isCrop;
    field("crop_custom").hidden = !(isCrop && v.crop === "Інше");
    document.getElementById("cargo-custom-field").hidden = cargoBase !== "Інше";

    document.getElementById("bigbag-field").hidden = v.size_type !== "Біг-бег";
    const volumeLabel = v.size_type === "Біг-бег" ? "Кількість біг-бегів, шт"
      : (v.size_type === "Насип" || v.size_type === "Рідкі") ? "Кількість, т" : "Вага, т";
    document.getElementById("volume-label").innerHTML = volumeLabel + ' <b class="req">*</b>';

    const isPeriod = v.date_type === "period";
    document.getElementById("date-to-field").hidden = !isPeriod;
    document.getElementById("date-from-label").innerHTML = (isPeriod ? "Початкова дата" : "Дата") + ' <b class="req">*</b>';

    form.querySelectorAll("[data-hide-liquid]").forEach(function (node) { node.hidden = isLiquid; });
    const unloadMethod = field("unload_method");
    if (v.size_type === "Насип" && !isLiquid) {
      unloadMethod.value = "Самоскид";
      unloadMethod.readOnly = true;
    } else if (unloadMethod.readOnly) {
      unloadMethod.readOnly = false;
      unloadMethod.value = "";
    }

    document.getElementById("unload-place-field").hidden = v.unload_cities.length > 1;
  }

  // ---------- fill / collect ----------

  function fillForm(data) {
    const v = state.values;
    ["department", "company", "vehicle_type", "cargo_type", "size_type"].forEach(function (key) {
      v[key] = data[key] || "";
    });
    const crops = state.config.crop_types;
    if (data.crop && crops.indexOf(data.crop) === -1) {
      v.crop = "Інше";
      field("crop_custom").value = data.crop;
    } else {
      v.crop = data.crop || "";
    }
    v.date_type = data.date_type === "period" ? "period" : "single";
    v.load_cities = (data.load_cities || []).slice();
    v.unload_cities = (data.unload_cities || []).slice();
    v.distribution = Object.assign({}, data.distribution || {});

    [
      "initiator", "company_custom", "vehicle_type_custom", "cargo_custom", "big_bag_weight", "volume",
      "notes", "date_from", "date_to", "load_place", "load_method", "load_contact",
      "unload_place", "unload_method", "unload_contact",
    ].forEach(function (key) {
      if (data[key] != null) field(key).value = data[key];
    });

    Object.keys(chipBoxes).forEach(syncChips);
    syncSegmented();
    pickers.load_cities.render();
    pickers.unload_cities.render();
    renderDistribution();
    updateVisibility();
  }

  function collect() {
    const v = state.values;
    const value = function (name) { return field(name).value.trim(); };
    return {
      department: v.department,
      initiator: value("initiator"),
      company: v.company,
      company_custom: value("company_custom"),
      vehicle_type: v.vehicle_type,
      vehicle_type_custom: value("vehicle_type_custom"),
      cargo_type: v.cargo_type,
      crop: v.crop === "Інше" ? value("crop_custom") : v.crop,
      cargo_custom: value("cargo_custom"),
      size_type: v.size_type,
      big_bag_weight: value("big_bag_weight"),
      volume: value("volume"),
      notes: value("notes"),
      date_type: v.date_type,
      date_from: value("date_from"),
      date_to: v.date_type === "period" ? value("date_to") : "",
      load_cities: v.load_cities,
      load_place: value("load_place"),
      load_method: value("load_method"),
      load_contact: value("load_contact"),
      unload_cities: v.unload_cities,
      unload_place: value("unload_place"),
      unload_method: value("unload_method"),
      unload_contact: value("unload_contact"),
      distribution: v.unload_cities.length > 1 ? v.distribution : {},
    };
  }

  // Легка перевірка на клієнті (остаточна — на сервері). Повертає перше повідомлення про помилку.
  function validate() {
    const v = state.values;
    const problems = [];
    function bad(node, message) {
      if (node) node.classList.add("invalid");
      problems.push({ node: node, message: message });
    }
    function requireChip(name, message) { if (!v[name]) bad(chipBoxes[name], message); }
    function requireInput(name, message) { if (!field(name).value.trim()) bad(field(name), message); }

    requireChip("department", "Оберіть, від кого запит");
    requireInput("initiator", "Вкажіть ініціатора заявки");
    requireChip("company", "Оберіть підприємство");
    if (v.company === "Інше") requireInput("company_custom", "Вкажіть назву підприємства");
    requireChip("cargo_type", "Оберіть вид вантажу");
    if (state.config.crop_cargo_types.indexOf(v.cargo_type) !== -1) {
      requireChip("crop", "Оберіть культуру");
      if (v.crop === "Інше") requireInput("crop_custom", "Вкажіть культуру");
    }
    if (v.cargo_type === "Інше") requireInput("cargo_custom", "Вкажіть тип вантажу");
    requireChip("size_type", "Оберіть габарит / негабарит");
    if (v.size_type === "Біг-бег" && !(num(field("big_bag_weight").value) > 0)) {
      bad(field("big_bag_weight"), "Вкажіть вагу 1 біг-бегу числом");
    }
    if (!(num(field("volume").value) > 0)) bad(field("volume"), "Вкажіть обсяг числом більше 0");
    requireChip("vehicle_type", "Оберіть тип авто");
    if (v.vehicle_type === "Інше") requireInput("vehicle_type_custom", "Вкажіть тип авто");
    requireInput("date_from", "Вкажіть дату перевезення");
    if (v.date_type === "period") {
      requireInput("date_to", "Вкажіть кінцеву дату періоду");
      if (field("date_to").value && field("date_from").value && field("date_to").value < field("date_from").value) {
        bad(field("date_to"), "Кінцева дата не може бути раніше початкової");
      }
    }
    if (!v.unload_cities.length) bad(pickers.unload_cities.input, "Додайте населений пункт розвантаження");
    if (v.unload_cities.length > 1) {
      const total = num(field("volume").value);
      if (total == null || Math.abs(distributionSum() - total) > 1e-6) {
        bad(document.getElementById("distribution"), "Сума розподілу має дорівнювати загальному обсягу");
      }
    }

    if (problems.length && problems[0].node) {
      problems[0].node.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return problems.length ? problems[0].message : null;
  }

  // ---------- submit ----------

  function setBusy(busy) {
    const fallback = document.getElementById("fallback-submit");
    fallback.disabled = busy;
    if (tg && tg.MainButton) {
      if (busy) tg.MainButton.showProgress(false);
      else tg.MainButton.hideProgress();
    }
  }

  async function submit() {
    if (state.submitting) return;
    clearError();
    const problem = validate();
    if (problem) {
      showError(problem);
      return;
    }
    state.submitting = true;
    setBusy(true);
    try {
      const result = await api("/api/requests", {
        method: "POST",
        body: JSON.stringify({ mode: mode, request_id: editId, source_id: copyId, form: collect() }),
      });
      form.hidden = true;
      if (tg && tg.MainButton) tg.MainButton.hide();
      doneBox.hidden = false;
      doneBox.textContent = (mode === "edit" ? "✅ Зміни збережено" : "✅ Заявку надіслано") +
        "\nID заявки: " + result.request_id;
      haptic("success");
      setTimeout(function () { if (tg) tg.close(); }, 1800);
    } catch (error) {
      showError(error.message, error.data);
    } finally {
      state.submitting = false;
      setBusy(false);
    }
  }

  // ---------- init ----------

  async function init() {
    if (tg) {
      tg.ready();
      tg.expand();
    }
    if (!tg || !tg.initData) {
      loading.hidden = true;
      showError("Відкрийте цю форму з Telegram-бота (кнопка «🧾 Заявка»).");
      return;
    }

    if (mode === "edit") {
      document.getElementById("page-title").textContent = "Редагування заявки " + editId;
      document.getElementById("page-subtitle").textContent = "Зміни з'являться в робочому чаті та таблиці";
    } else if (mode === "copy") {
      document.getElementById("page-title").textContent = "Нова заявка на основі " + copyId;
      document.getElementById("page-subtitle").textContent = "Отримає новий номер. Перевірте дату та обсяг";
    }

    try {
      const config = await api("/api/config");
      state.config = config;

      renderChips("department", config.departments);
      renderChips("company", config.companies);
      renderChips("vehicle_type", config.vehicle_types);
      renderChips("cargo_type", config.cargo_types);
      renderChips("crop", config.crop_types.concat(["Інше"]));
      renderChips("size_type", config.size_types);
      initSegmented();
      createCityPicker("load_cities");
      createCityPicker("unload_cities");

      const datalist = document.getElementById("contacts");
      config.contacts.forEach(function (contact) {
        const option = el("option");
        option.value = contact;
        datalist.appendChild(option);
      });

      if (!config.is_admin) {
        field("date_from").min = config.today;
        field("date_to").min = config.today;
      }

      if (mode === "create") {
        field("initiator").value = config.last_initiator || "";
      } else {
        const response = await api(
          "/api/requests/" + encodeURIComponent(editId || copyId) + "?mode=" + (mode === "edit" ? "edit" : "copy")
        );
        fillForm(response.form);
      }

      field("volume").addEventListener("input", updateDistributionSum);
      form.querySelectorAll("input, textarea").forEach(function (node) {
        node.addEventListener("input", function () { node.classList.remove("invalid"); });
      });
      updateVisibility();

      loading.hidden = true;
      form.hidden = false;
      if (tg.MainButton) {
        tg.MainButton.setText(mode === "edit" ? "Зберегти зміни" : "Надіслати заявку");
        tg.MainButton.onClick(submit);
        tg.MainButton.show();
      } else {
        document.getElementById("fallback-submit").hidden = false;
      }
    } catch (error) {
      loading.hidden = true;
      showError(error.message, error.data);
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    submit();
  });

  init();
})();
