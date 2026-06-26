// PM2 ecosystem for the Roblox account generator.
//
//   pm2 start ecosystem.config.js                # start all apps
//   pm2 start ecosystem.config.js --only roblox-generator
//   pm2 logs roblox-generator
//   pm2 restart roblox-generator
//   pm2 stop roblox-generator
//   pm2 delete roblox-generator
//   pm2 save
//   pm2 startup

module.exports = {
  apps: [
    {
      name: 'roblox-generator',
      cwd: '/root/Roblox-Account-Creator',
      script: 'main.py',
      interpreter: '/root/Roblox-Account-Creator/venv/bin/python3.11',
      autorestart: false,
      out_file: '/root/Roblox-Account-Creator/output/pm2.out.log',
      error_file: '/root/Roblox-Account-Creator/output/pm2.err.log',
      merge_logs: true,
      time: true,
      env: {
        PYTHONUNBUFFERED: '1',
        PYTHONDONTWRITEBYTECODE: '1',
        TERM: 'xterm-256color',
        COLORTERM: 'truecolor',
        FORCE_COLOR: '1',
        CLICOLOR_FORCE: '1',
        NO_COLOR: '',
        // AntiCaptcha solves from their own IP pool (no slow residential proxy in task).
        ANTICAPTCHA_USE_PROXY: '0',
      },
    },

    // Optional: Medal-bot Roblox quest pipeline
    //   pm2 start ecosystem.config.js --only medal-roblox-quest
    {
      name: 'medal-roblox-quest',
      cwd: '/root/medalbot',
      script: 'roblox_quest.js',
      interpreter: 'node',
      autorestart: false,
      out_file: '/root/medalbot/logs/quest.out.log',
      error_file: '/root/medalbot/logs/quest.err.log',
      merge_logs: true,
      time: true,
      env: {
        NODE_ENV: 'production',
        FORCE_COLOR: '1',
        TERM: 'xterm-256color',
        COLORTERM: 'truecolor',
        NO_COLOR: '',
      },
    },
  ],
};
