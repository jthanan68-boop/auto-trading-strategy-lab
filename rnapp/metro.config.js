const { getDefaultConfig } = require("metro-config");

const config = getDefaultConfig(__dirname);

config.resolver = {
  ...config.resolver,
  assetExts: config.resolver.assetExts.filter(
    (ext) => ext !== "svg" // we use react-native-svg
  ),
};

module.exports = config;