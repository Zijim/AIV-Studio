# MiniMax H3 I2VA 首帧写作指引
本指南适配fl2va原资料的I2VA与共享写作部分。当前没有FL2VA、L2VA、T2VA、ref2va能力；不能因为资料描述这些模式就输出对应输入关系。
执行首帧已由asset_binding_map确定为<Picture 1>。先根据实际附图建立风格、画面构图、可见主体和姿态、场景锚点，再描述向前发展的动作，不把首帧重新写成另一画面。
按“首帧锚点→动作起始→连续中间变化→结果/反应”写完整过程。描述谁用哪只手握住哪个部位、物体如何移动和接触、身体重心如何配合、动作怎样结束。以实际可见内容为依据；遮挡部位、不清楚的武器或未知地面不能写成观察事实。
保持可见身份、服装、材质、主要颜色、关键物体与空间关系。若用户要求另一开场构图或道具而当前首帧不相容，先clarify或建议首帧编辑，不靠“忽略三视图”“只生成一个角色”假装改变首帧条件。
本阶段video参数中的时长、比例、MP和输入已冻结，不能自行更改。根据resolved_seconds、resolved_frames安排可完成的连续动作，避免在3秒内塞入拔剑、挥砍、插地、转身、退场等独立事件。
输出prompt_document的shots、overall_soundscape和non_diegetic_music。每个shot的description写完整自然英文，不含[Shot N]或固定首帧指令；这些由编译器生成。中文说明需准确概括正文实际落实的内容。
默认单镜头Static Shot。用户明确要求运动时写准运动类型、必要幅度和速度；不得把Zoom与Push混用。不自行增加切镜。
动作音效可与动作同步，overall_soundscape总结物理声音与环境；默认non_diegetic_music为N/A。完全静音时两项为N/A且正文不出现发声动作。无用户台词不加对白、呼喊或旁白。
literal_texts逐字在相应镜头出现。正文描述真实可见特征而非套用“保持面部、发型”到全封闭头盔角色。字数由内容决定，信息完整但不重复堆叠。最终完整prompt含固定前缀不超过工具字符上限。
编译的目标格式是首帧指令第一行，然后integrated_multimodal_description、overall_soundscape、non_diegetic_music；绝不能输出ref2va的六分节代替它。
