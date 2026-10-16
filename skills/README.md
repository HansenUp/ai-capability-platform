# Skills（Agent Skills 开放规范）

每个子目录为一个技能包：`SKILL.md`（frontmatter 含 `name`、`description`）+ 可选 `scripts/`、`references/`、`assets/`。
上架流水线：静态扫描（提示注入 / 危险指令 / 远程拉取）→ 评测回放 → 人审 → cosign 签名 → 商店登记。
硬规则：禁止运行时远程拉取可执行内容；必须声明所需能力；必须自带评测用例。
