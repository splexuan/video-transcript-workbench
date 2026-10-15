import type { RecognitionModel } from '../types'

/**
 * 真正参与语音识别的引擎。
 *
 * 模型目录是全类型的：`punctuation` 只服务「智能分句」、`tts` 把文案读成音频，
 * 两者都不参与识别。首页的「识别模型」选择和设置页的「默认识别模型」只能列这几个，
 * 否则用户会选到一个根本不能转写的模型——界面上还会按 tier 把它讲成「精准时间轴」。
 *
 * 目前只开放 SenseVoice：两个 faster-whisper 模型（small / medium）不在界面上暴露，
 * 后端的下载与识别能力仍然保留，需要时把 'faster_whisper' 加回这里即可。
 */
export const RECOGNITION_ENGINES = ['sensevoice'] as const

/** 从全量模型里挑出识别模型；调用方拿到的列表可能为空或缺省。 */
export function recognitionModels(models: RecognitionModel[] | undefined | null): RecognitionModel[] {
  return (models ?? []).filter((model) =>
    (RECOGNITION_ENGINES as readonly string[]).includes(model.engine),
  )
}
