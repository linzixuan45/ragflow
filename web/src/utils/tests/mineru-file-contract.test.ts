import { FileMimeType, Images, PreviewableImages } from '@/constants/common';
import { FileIconMap } from '@/constants/file';
import {
  DefaultParseDocumentType,
  ParseDocumentType,
} from '@/constants/parser';
import { isSupportedPreviewDocumentType } from '@/utils/document-util';

describe('MinerU file contract', () => {
  it('uses MinerU for newly created parser configurations', () => {
    expect(DefaultParseDocumentType).toBe(ParseDocumentType.MinerU);
  });

  it('recognizes browser-previewable image formats', () => {
    expect(Images).toEqual(
      expect.arrayContaining(['png', 'jpeg', 'jpg', 'webp', 'gif', 'bmp']),
    );
    expect(FileMimeType.Jp2).toBe('image/jp2');
    expect(FileMimeType.Webp).toBe('image/webp');
    expect(Images).toEqual(expect.arrayContaining(['jp2', 'tif', 'tiff']));
  });

  it('does not expose JP2 and TIFF as browser previews', () => {
    expect(isSupportedPreviewDocumentType('jp2')).toBe(false);
    expect(isSupportedPreviewDocumentType('tif')).toBe(false);
    expect(isSupportedPreviewDocumentType('tiff')).toBe(false);
    expect(PreviewableImages).not.toEqual(
      expect.arrayContaining(['jp2', 'tif', 'tiff']),
    );
  });

  it('provides file icons for every MinerU image extension', () => {
    for (const extension of [
      'png',
      'jpeg',
      'jpg',
      'jp2',
      'webp',
      'gif',
      'bmp',
      'tiff',
    ]) {
      expect(FileIconMap[extension as keyof typeof FileIconMap]).toBeTruthy();
    }
  });
});
