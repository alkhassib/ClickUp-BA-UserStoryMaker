## 📌 User Story
> **As a** [role], **I want** [action], **so that** [value for the user or business].

---

## 🚦 Preconditions
- [Login state and role]
- [Record / request state before start]
- [Data that must already exist]

## ▶️ Trigger & Entry Point
`[Screen]` › `[Menu]` › `[Button]` — or [scheduled event / system call]

## 🔗 Dependencies & Components
| Type | ID | Note |
|---|---|---|
| Story / Integration | [US-XXX-NN / INT-nn] | [N/A + reason] |
| Component / Global Rule | [CMP-nn / GR-nn] | |

---

## 🧭 Scenarios
| ID | Type | Steps |
|---|---|---|
| **SC-01** | 🟢 Main | 1. … 2. … 3. … |
| **SC-02** | 🔵 Alternative | … |
| **SC-03** | 🔴 Exception | … |

## ⚖️ Business Rules
| ID | Rule (If … then …) | Effect | Message |
|---|---|---|---|
| **BR-01** | If …, the system … | ST / NUM / NTF / AUD | MSG-nnn |

---

## 🧾 Field Table
| # | Field (En / عربي) | Type | Required | Editable | Default | Constraints | Source | Visibility, Rules & Messages |
|---|---|---|---|---|---|---|---|---|
| 1 | [Field Name] / [اسم الحقل] | Text | Yes | Yes | None | [length, format] | Input / LKP-nnn | BR-nn · MSG-nnn |

## 🔘 Buttons & Actions
| Button | Visible when | Enabled when | On click (in order) | Confirmation | Navigates to |
|---|---|---|---|---|---|
| [Button] | Always | [condition] | 1. … 2. … | MSG-nnn | [Next screen] |

## 💬 Messages & Notifications
| ID | Text (copied from repository) | Shown when |
|---|---|---|
| **MSG-nnn** | "…" | BR-nn |

## 🔐 Permissions
| Role | Sees | Executes | Behavior for unauthorized |
|---|---|---|---|
| [role code] | ✅ | ✅ | — |
| [role code] | ❌ | ❌ | Hidden / Disabled / MSG-nnn |

## ⏱️ Timeouts & Scheduled Events
| Event | Formula / Duration | Setting | Result |
|---|---|---|---|
| [N/A + reason] | | CFG-nnn | ST-nn / NTF-nnn |

---

## ✅ Acceptance Criteria
| ID | Given | When | Then | Rule | Type |
|---|---|---|---|---|---|
| **AC-01** | … | … | … | BR-01 | 🟢 Positive |
| **AC-02** | … | … | … | BR-01 | 🔴 Negative |
| **AC-03** | … | … | … | BR-02 | 🟡 Boundary |

## 🧪 Examples & Test Data
> 📅 **Assumed today:** DD/MM/YYYY — **Baseline valid values:** [valid value per field]

| ID | Input (difference only) | Expected result | Criterion |
|---|---|---|---|
| **EX-01** | Baseline as-is | Accepted → go to … | AC-01 |
| **EX-02** | [boundary value] | MSG-nnn | AC-02 |

---

## 📤 Outputs & Post-State
- **Saved:** …
- **New status:** `ST-nn`
- **Generated numbers:** `NUM-nn`
- **Notifications:** `NTF-nnn`
- **Audit:** `AUD-nn`
- **Where the effect appears:** …

## 📐 Non-Functional Requirements
- [NFR-nn or N/A + reason]

## 🎨 Design
🔗 [UI-nn: design link] — States covered: Empty · Loading · Error · Success

## 🚫 Out of Scope
- [Not covered → where it is covered]

## ❓ Open Questions
- [Q-nn: question] or None

---

## 🏁 Definition of Ready
- [ ] All sections filled or marked N/A with reason
- [ ] Role exists in roles table and permissions matrix
- [ ] Every rule has ≥1 AC, every AC has a concrete example
- [ ] Every referenced message, setting and lookup exists in its repository
- [ ] No open questions affecting behavior
- [ ] Design attached and matches field table and screen states
- [ ] Dependencies ready; integration mocks available
- [ ] Three-amigos review (BA · QA · Dev)

📆 Review date: [ ] — 👥 Reviewers: [ ]
