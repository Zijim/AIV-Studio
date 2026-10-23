# Qwen 双图参考编辑指引
先区分source_image主图和reference_image参考图；描述参考图提供的是人物、服装、色彩、材质、风格或构图中的哪一项。
instruction明确从参考图迁移什么、主图保留什么，防止同时复制无关人物和背景。不能因为有两张图就默认换脸或混合角色；职责不明先澄清。
使用与实际图像输入顺序一致的“first/source image”“second/reference image”表达，不编造当前工具未定义的Subject或Picture执行标签。
keep、changes及negative_prompt与instruction一致。用户要求只换服装时，保留主图人物身份、姿态和背景，不复制参考图整张画面。尺寸和质量分支由工具决定。
