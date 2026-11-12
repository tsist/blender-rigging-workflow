# 公开分发、原始快照与复现

仓库更新版为 **0.40.0-r3**，运行插件内部版本为 **0.40.0 local-r3**。它保留原验收快照，不在上传时修改运行逻辑。项目作者代码以GPL-3.0-or-later发布，原有历史标签／源码保留。

## 原始字节与本机路径

183个运行文件、165个Python及runtime manifest均与本轮已验收包一致。包内README、REVIEW-ACCEPTANCE和第三方声明里的“本机／尚未公开发布”属于原快照形成时的记录；当前公开状态以仓库根README、PUBLICATION.json及本页为准。

八个声明文件包含原测试机路径默认值或来源路径，PUBLICATION.json记录其路径名单与整个文件的SHA。它们不包含角色数据、原始审计正文、账号凭据或密钥。运行时可编辑的CLI／MMD／OpenCC路径须由使用者明确选择；来源路径不代表可访问公共资产。没有把私有模板、定位图谱、角色、字体、纹理或样本动作加入发布。

打包检查仍拒绝其他文件出现本机路径。只有八个明确列出、与已审文件逐字节一致的路径文件可保留；运行payload的每个文件都须与固定清单一致。这不是允许任意新路径或跳过来源检查。

## 第三方资源

唯一PMX资源是未经修改的Uka骨架／IK参考，CC BY 4.0，固定上游commit与SHA；署名、上游使用指引和许可说明在资源侧车中。没有纹理，不是完整视觉模型。其CC BY许可不被项目GPL替代。

pycpd为MIT，原许可证随文件保留。MMD Tools／OpenCC／Blender／blenderctl为外部运行依赖，不随源码打包，不自动安装。详见根与运行包的第三方声明。

## 复现打包

在干净checkout中运行 `python -B scripts/build_release.py`，可生成源码、技能和与原包成员逐字节一致的插件ZIP及SHA256SUMS。压缩容器时间等元数据不同，重建ZIP本身的SHA可能不同，不能冒充原已验压缩包SHA。

维护者发布时使用 `--accepted-addon <已验ZIP绝对路径>`：先验证原ZIP SHA、唯一安全成员集合和所有183文件字节，再复制原包作为安装附件。原已验ZIP的SHA在PUBLICATION.json与VALIDATION.md中。输出目录非空时不覆盖已有附件。

源码CI只做解析、固定清单、禁止内容和归档完整性检查。它不启动Blender，不代替本机GUI／角色验收。`scripts/smoke_blender.py`与`scripts/smoke_addon_archive.py`可在独立factory-startup Blender执行有限注册检查；禁止打开用户当前工程或把注册成功称作制作质量通过。

本次更新沿用公开预发布流程。未替换用户日常安装，不包含新的收费调用、资产上传或服务自动配置。
