# Qwen 局部编辑

移除小物件或修补瑕疵时，在原图上定位需要改动的最小矩形，覆盖目标及需要重建的边缘。

- 以原图左上为起点，填写 parameters 中的 region_x、region_y、region_width、region_height。横向按原图宽度归一化，纵向按原图高度归一化。坐标加宽高须落在原图内。
- 需要放大确认目标时，使用 inspect_media 裁切复看。把局部观察中的位置换算回原图坐标后再提交。目标有歧义时先澄清具体对象。
- 在 prompt_document.instruction 描述要移除或修复的目标，以及应补成的纹理、光线和边缘。保持项写入 keep，具体改动写入 changes。默认采样使用 4 步 Lightning LoRA，主要约束写入正向指令。
- 用成品核对目标是否已处理。成品为原图尺寸，适合与原图同位置比较。
