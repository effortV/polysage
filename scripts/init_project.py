"""初始化项目默认文件：任务书、约束、过关规则、base 占位、原料库与假设价、首版 Top 20、数据表模板。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polysage import kb  # noqa: E402
from polysage.formulation import constraints as C  # noqa: E402
from polysage.formulation import library as L  # noqa: E402
from polysage.formulation import materials as MAT  # noqa: E402
from polysage.ml import dataset as D  # noqa: E402
from polysage.pipeline import task  # noqa: E402


def main() -> None:
    t = task.load()
    c = C.load()
    if not kb.PROJECT_FILES["rules"].exists():
        kb.write_text(kb.PROJECT_FILES["rules"],
                      "# 过关规则（建议，需与甲方确认）\n\n"
                      "- base = 现用膜各项的算术平均值，同时记录标准差（SD）\n"
                      "- 拉伸、撕裂、穿刺、热封强度 ≥ base × 0.95 且 ≥ base − 1 SD\n"
                      "- 雾度 ≤ base + 1.0 个百分点；透光率 ≥ base − 1%\n"
                      "- 热封窗口不窄于现状；外观不劣于现状\n"
                      "- 五项全部满足才算过关；不能靠加厚补强，不能用一项提高抵消另一项下降\n")
    if not kb.PROJECT_FILES["base"].exists():
        kb.write_text(kb.PROJECT_FILES["base"],
                      "# base（现用膜实测）\n\n尚未测试。Step 0 完成后在此填写：膜厚、拉伸(MD/TD)、撕裂(MD/TD)、穿刺、落镖、热封(T−10/T/T+10)、雾度、透光率，"
                      "均值 ± SD 与条数；原料表征（MFR、密度、DSC、灰分）。\n")
    n_m = MAT.seed_materials()
    n_f = L.seed_top20()
    D.write_template()
    print(f"任务书: {task.TASK_PATH}\n约束: {kb.PROJECT_FILES['constraints']}\n新增材料 {n_m}，新增配方 {n_f}\n数据表模板: {D.TEMPLATE_PATH}")


if __name__ == "__main__":
    main()
